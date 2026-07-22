from __future__ import annotations

import threading
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import cards
import commands
from config import Config, ConfigError
from handlers import BaseHandler
from services.ai_runner import AIRunner
from services.messenger import FeishuApiError, Messenger
from services.prompt_builder import build_prompt, extract_text, is_standalone_file_message, normalize_event
from services.session_store import SessionStore
from utils.file_delivery import extract_file_deliveries, is_image_file
from utils.lark_cli_wrapper import LarkCliError
import logger


@dataclass(frozen=True)
class Conversation:
    session_id: str | None
    tool: str
    model: str
    resume: bool


class MessageHandler(BaseHandler):
    def __init__(
        self,
        config: Config,
        messenger: Messenger,
        ai_runner: AIRunner,
        session_store: SessionStore,
        scheduler: object,
        *,
        bot_open_id: str = "",
    ) -> None:
        self.config = config
        self.messenger = messenger
        self.ai_runner = ai_runner
        self.session_store = session_store
        self.scheduler = scheduler
        self._bot_open_id = bot_open_id.strip()
        self._seen: deque[str] = deque(maxlen=1000)
        self._seen_set: set[str] = set()
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()

    def handle_event(self, event: dict[str, Any]) -> None:
        message = normalize_event(event)
        message_id = message.get("message_id")
        if not message_id:
            logger.warning("收到的消息事件缺少 message_id，已忽略")
            return
        if self._is_duplicate(message_id):
            logger.debug("忽略重复消息：消息=%s", message_id)
            return
        if message.get("sender_type") in {"bot", "app"}:
            logger.debug("忽略机器人或应用消息：消息=%s", message_id)
            return
        if not self._should_handle(message):
            logger.debug("忽略未 @ 当前机器人的群消息：消息=%s", message_id)
            return
        if is_standalone_file_message(message):
            logger.info("收到独立文件消息，等待用户回复文件后再处理：消息=%s", message_id)
            return

        text = extract_text(message).strip()
        logger.info(
            "收到用户消息：消息=%s 发送者=%s 类型=%s 聊天=%s 内容=%s",
            message_id,
            message.get("sender_id") or "unknown",
            message.get("message_type"),
            message.get("chat_type"),
            text[:80] or "（非文本）",
        )
        ctx = commands.CommandContext(
            text=text,
            message=message,
            config=self.config,
            session_store=self.session_store,
            ai_runner=self.ai_runner,
            scheduler=self.scheduler,
            messenger=self.messenger,
        )
        command_response = commands.registry.match(ctx)
        if command_response:
            logger.info("匹配到用户命令：消息=%s 命令=%s", message_id, text.split()[0] if text else text)
            self._reply_card(message, command_response.card)
            return

        try:
            conversation = self._conversation_for(message)
        except ConfigError as exc:
            logger.exception("选择 AI 会话配置失败：消息=%s", message_id)
            self._reply_card(message, cards.build_error_card("会话配置错误", str(exc)))
            return
        if not conversation:
            self._reply_card(
                message,
                cards.build_error_card(
                    "话题会话未找到",
                    "当前话题未关联到机器人回复，无法安全续接会话。请在机器人回复卡片下创建话题。",
                ),
            )
            return

        try:
            prompt_result = build_prompt(message, self.messenger, self.config)
        except FeishuApiError as exc:
            logger.exception("构建 AI 提示词失败：消息=%s", message_id)
            self._reply_card(message, cards.build_error_card("上下文构建失败", str(exc)))
            return

        logger.info(
            "启动 AI 处理线程：消息=%s CLI=%s 模型=%s 续接=%s",
            message_id,
            conversation.tool,
            conversation.model,
            conversation.resume,
        )
        threading.Thread(
            target=self._run_ai_and_reply,
            args=(message, prompt_result.prompt, prompt_result.work_dir, conversation),
            name=f"ai-{message_id}",
            daemon=True,
        ).start()

    def _run_ai_and_reply(
        self,
        message: dict[str, Any],
        prompt: str,
        delivery_dir: Path,
        conversation: Conversation,
    ) -> None:
        message_id = message["message_id"]
        lock = self._get_session_lock(conversation.session_id) if conversation.session_id else nullcontext()
        with lock:
            reaction_id = self._add_processing_reaction(message_id)
            result = None
            try:
                result = self.ai_runner.run(
                    prompt,
                    message_id,
                    tool=conversation.tool,
                    model=conversation.model,
                    session_id=None if conversation.resume else conversation.session_id,
                    resume_session_id=conversation.session_id if conversation.resume else None,
                )
                if result.ok:
                    logger.info(
                        "AI 处理完成：消息=%s 会话=%s 回复长度=%s",
                        message_id,
                        result.session_id or "无",
                        len(result.result),
                    )
                else:
                    logger.warning(
                        "AI 处理失败：消息=%s 错误=%s 退出码=%s",
                        message_id,
                        result.error[:200] if result.error else "未知",
                        result.exit_code,
                    )
                if result.ok:
                    delivery = extract_file_deliveries(result.result, delivery_dir)
                    if delivery.paths:
                        self._reply_deliveries(message, delivery.paths, delivery.text, result, conversation)
                    else:
                        result_text = delivery.text
                        if delivery.errors:
                            result_text = "\n".join([result_text, *[f"文件交付失败：{error}" for error in delivery.errors]]).strip()
                        card = cards.build_ai_card(result.tool, result.model, result_text or "任务已完成。", result.thinking)
                        try:
                            reply = self.messenger.reply_card(message_id, card, reply_in_thread=self._reply_in_thread(message))
                        except FeishuApiError:
                            logger.exception("发送 AI 回复卡片失败：消息=%s", message_id)
                            reply = None
                        if reply and result.session_id:
                            self._save_session_mappings(message, reply, conversation, result.session_id)
                else:
                    card = cards.build_error_card("AI 分析失败", result.error or "未能获取有效输出", result.stderr)
                    try:
                        self.messenger.reply_card(message_id, card, reply_in_thread=self._reply_in_thread(message))
                    except FeishuApiError:
                        logger.exception("发送 AI 回复卡片失败：消息=%s", message_id)
            except Exception:
                logger.exception("AI 处理线程发生未预期异常：消息=%s", message_id)
                try:
                    self.messenger.reply_card(
                        message_id,
                        cards.build_error_card("服务内部错误", "AI 处理过程中发生未预期异常，请重试。"),
                        reply_in_thread=self._reply_in_thread(message),
                    )
                except Exception:
                    logger.exception("发送错误卡片也失败：消息=%s", message_id)
            finally:
                self._delete_reaction(message_id, reaction_id)

            if result and result.ok and result.session_id:
                self._auto_resume_memory(
                    result.session_id,
                    conversation.tool,
                    conversation.model,
                    conversation.resume,
                    message_id,
                )

    def _save_session_mappings(
        self,
        user_message: dict[str, Any],
        reply: dict[str, Any],
        conversation: Conversation,
        session_id: str,
        *,
        related_bot_message_ids: list[str] | None = None,
    ) -> None:
        metadata = {
            "source_message_id": user_message.get("message_id"),
            "reply_message_id": reply.get("message_id"),
            "root_message_id": user_message.get("root_id"),
            "parent_message_id": user_message.get("parent_id"),
            "thread_id": user_message.get("thread_id"),
        }
        source_message_id = user_message.get("message_id")
        if source_message_id:
            self.session_store.set(
                source_message_id,
                session_id,
                tool=conversation.tool,
                model=conversation.model,
                link_type="user_message",
                metadata=metadata,
            )
        reply_message_id = reply.get("message_id")
        if reply_message_id:
            self.session_store.set(
                reply_message_id,
                session_id,
                tool=conversation.tool,
                model=conversation.model,
                link_type="bot_reply",
                metadata=metadata,
            )
        for related_message_id in related_bot_message_ids or []:
            if related_message_id:
                self.session_store.set(
                    related_message_id,
                    session_id,
                    tool=conversation.tool,
                    model=conversation.model,
                    link_type="bot_file",
                    metadata=metadata,
                )
        if not user_message.get("thread_id"):
            return
        seen_keys: set[str] = set()
        for key in (reply.get("thread_id"), user_message.get("thread_id")):
            if key in seen_keys:
                continue
            if key:
                seen_keys.add(key)
                self.session_store.set(
                    key,
                    session_id,
                    tool=conversation.tool,
                    model=conversation.model,
                    link_type="thread",
                    metadata=metadata,
                )

    def _reply_deliveries(
        self,
        message: dict[str, Any],
        paths: list[Path],
        completion_text: str,
        result: Any,
        conversation: Conversation,
    ) -> None:
        message_id = message["message_id"]
        reply_in_thread = self._reply_in_thread(message)
        delivered_message_ids: list[str] = []
        try:
            for path in paths:
                if is_image_file(path):
                    try:
                        delivered = self.messenger.reply_image(message_id, path, reply_in_thread=reply_in_thread)
                    except LarkCliError as exc:
                        logger.warning("图片发送失败，回退为文件发送：消息=%s 路径=%s 错误=%s", message_id, path, exc)
                        delivered = self.messenger.reply_file(message_id, path, reply_in_thread=reply_in_thread)
                else:
                    delivered = self.messenger.reply_file(message_id, path, reply_in_thread=reply_in_thread)
                delivered_message_id = delivered.get("message_id")
                if not isinstance(delivered_message_id, str) or not delivered_message_id:
                    raise LarkCliError(f"发送文件后未返回消息 ID：{path}")
                delivered_message_ids.append(delivered_message_id)
                logger.info("已发送 AI 交付文件：请求消息=%s 文件消息=%s 路径=%s", message_id, delivered_message_id, path)

            status = completion_text or "文件已生成并发送。"
            card = cards.build_ai_card(result.tool, result.model, status, result.thinking)
            reply = self.messenger.reply_card(delivered_message_ids[-1], card, reply_in_thread=reply_in_thread)
        except (FeishuApiError, LarkCliError) as exc:
            logger.exception("发送 AI 交付文件失败：消息=%s 错误=%s", message_id, exc)
            self._reply_card(message, cards.build_error_card("文件发送失败", str(exc)))
            return
        if result.session_id:
            self._save_session_mappings(
                message,
                reply,
                conversation,
                result.session_id,
                related_bot_message_ids=delivered_message_ids,
            )
        logger.info("文件交付完成说明已发送：文件消息=%s 说明消息=%s", delivered_message_ids[-1], reply.get("message_id"))

    def _conversation_for(self, message: dict[str, Any]) -> Conversation | None:
        if message.get("thread_id"):
            resume_info = self.session_store.resolve_for_thread(message)
            if not resume_info:
                return None
            session_id = str(resume_info.get("session_id") or "").strip()
            tool = str(resume_info.get("tool") or "").strip()
            model = str(resume_info.get("model") or "").strip()
            if not session_id or not tool or not model:
                logger.warning(
                    "持久化的话题会话信息不完整：消息=%s 话题=%s",
                    message.get("message_id"),
                    message.get("thread_id"),
                )
                return None
            logger.debug("解析到话题续接会话：消息=%s 会话=%s", message.get("message_id"), session_id)
            return Conversation(session_id=session_id, tool=tool, model=model, resume=True)

        tool = self.config.current_tool()
        model = self.config.resolve_model(tool)
        session_id = str(uuid4()) if self.ai_runner.supports_explicit_session(tool) else None
        logger.debug("创建新 AI 会话：消息=%s 会话=%s", message.get("message_id"), session_id or "由 CLI 输出确定")
        return Conversation(session_id=session_id, tool=tool, model=model, resume=False)

    def _should_handle(self, message: dict[str, Any]) -> bool:
        if message.get("chat_type") != "group":
            return True

        bot_open_id = self._bot_open_id
        if not bot_open_id:
            try:
                bot_open_id = self.messenger.get_bot_open_id()
            except FeishuApiError as exc:
                logger.warning("无法获取机器人 open_id，忽略群消息：%s", exc)
                return False
            if bot_open_id:
                self._bot_open_id = bot_open_id
        if not bot_open_id:
            logger.warning("无法获取机器人 open_id，忽略群消息")
            return False

        mentions = message.get("mentions")
        if not isinstance(mentions, list):
            return False
        return any(bot_open_id in self._mention_ids(item) for item in mentions if isinstance(item, dict))

    @staticmethod
    def _mention_ids(mention: dict[str, Any]) -> set[str]:
        candidates: set[str] = set()
        for key in ("open_id", "user_id", "union_id"):
            value = mention.get(key)
            if isinstance(value, str) and value:
                candidates.add(value)
        mention_id = mention.get("id")
        if isinstance(mention_id, dict):
            for key in ("open_id", "user_id", "union_id"):
                value = mention_id.get(key)
                if isinstance(value, str) and value:
                    candidates.add(value)
        elif isinstance(mention_id, str) and mention_id:
            candidates.add(mention_id)
        return candidates

    def _reply_card(self, message: dict[str, Any], card: dict[str, Any]) -> None:
        try:
            self.messenger.reply_card(message["message_id"], card, reply_in_thread=self._reply_in_thread(message))
        except FeishuApiError:
            logger.exception("回复卡片失败：消息=%s", message.get("message_id"))

    @staticmethod
    def _reply_in_thread(message: dict[str, Any]) -> bool:
        return bool(message.get("thread_id"))

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _auto_resume_memory(
        self,
        session_id: str,
        tool: str,
        model: str,
        is_topic: bool,
        source_message_id: str,
    ) -> None:
        if not self.config.get("ai.auto_memory_resume", True):
            return
        prompt = self._build_memory_prompt(is_topic)
        timeout = int(self.config.get("ai.auto_memory_resume_timeout_seconds", 300))
        memory_message_id = f"auto-memory-{session_id}-{source_message_id}"
        logger.info("触发自动记忆 resume：会话=%s 来源消息=%s", session_id, source_message_id)
        try:
            result = self.ai_runner.run(
                prompt,
                memory_message_id,
                tool=tool,
                model=model,
                resume_session_id=session_id,
                timeout_seconds=timeout,
                register=False,
            )
            if not result.ok:
                logger.warning("自动记忆 resume 失败：会话=%s 错误=%s", session_id, result.error)
        except Exception as exc:
            logger.warning("自动记忆 resume 异常：会话=%s 错误=%s", session_id, exc)

    @staticmethod
    def _build_memory_prompt(is_topic: bool) -> str:
        return f"""【来自系统的自动提示】
{ "看起来用户刚才又进行了追问，且你已完成" if is_topic else "你已经完成了一次用户的任务/指令/问题" }。请检查是否有按照记忆系统的要求，更新完善记忆文件。
如果已按照要求更新，请直接忽略本次提醒！
如果尚未更新，请按照格式要求更新短期记忆，判断是否需要更新或者整理长期记忆，以及是否有识别到用户要求/偏好并需要更新SOUL。
注意事项：
1. 为了避免遗忘，你**务必**再次回顾系统提示词中，关于记忆系统的约束，明确你需要做什么，应当如何更新这三个记忆文件。
2. 记忆系统的核心要求是，简明扼要！沉淀经验！可以追溯！语言要极尽精简凝练，你必须用最少的文字，记录最清晰完整的意思，最核心的内容。避免记忆迅速膨胀，避免记忆变成操作日志。
3. 如果你认为本次没有值得更新到记忆的东西，则相信你自己，你有权选择不更新。记忆要宁缺毋滥！
4. 记忆系统是需要维护的，你还需要判断短期记忆是否需要清理（一般两个月以前的可以清理了）、长期记忆是否有过时、是否有遵循简明扼要等核心原则、是否需要更新整理等等。
"""

    def _add_processing_reaction(self, message_id: str) -> str | None:
        emoji = self.config.get("feishu.reaction_processing", "")
        if not emoji:
            return None
        try:
            reaction_id = self.messenger.add_reaction(message_id, emoji)
            logger.debug("已添加 OnIt 表情：消息=%s 表情=%s", message_id, emoji)
            return reaction_id
        except FeishuApiError as exc:
            logger.warning("添加 OnIt 表情失败：消息=%s 错误=%s", message_id, exc)
            return None

    def _delete_reaction(self, message_id: str, reaction_id: str | None) -> None:
        if not reaction_id:
            return
        try:
            self.messenger.delete_reaction(message_id, reaction_id)
            logger.debug("已移除 OnIt 表情：消息=%s 表情=%s", message_id, reaction_id)
        except FeishuApiError as exc:
            logger.warning("移除 OnIt 表情失败：消息=%s 表情=%s 错误=%s", message_id, reaction_id, exc)

    def _is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return False
