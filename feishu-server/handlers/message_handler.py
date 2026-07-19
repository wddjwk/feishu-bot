from __future__ import annotations

import logging
import threading
from collections import deque
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


logger = logging.getLogger(__name__)


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

    def handle_event(self, event: dict[str, Any]) -> None:
        message = normalize_event(event)
        message_id = message.get("message_id")
        if not message_id:
            logger.warning("收到的消息事件缺少 message_id，已忽略")
            return
        if self._is_duplicate(message_id):
            logger.info("忽略重复消息：消息=%s", message_id)
            return
        if message.get("sender_type") in {"bot", "app"}:
            logger.info("忽略机器人或应用消息：消息=%s", message_id)
            return
        if not self._should_handle(message):
            logger.info("忽略未 @ 当前机器人的群消息：消息=%s", message_id)
            return
        if is_standalone_file_message(message):
            logger.info("收到独立文件消息，等待用户回复文件后再处理：消息=%s", message_id)
            return

        text = extract_text(message).strip()
        logger.info(
            "收到用户消息：消息=%s 类型=%s 聊天类型=%s 话题=%s 根消息=%s 父消息=%s 发送者类型=%s 发送者=%s 文本长度=%s",
            message_id,
            message.get("message_type"),
            message.get("chat_type"),
            message.get("thread_id"),
            message.get("root_id"),
            message.get("parent_id"),
            message.get("sender_type"),
            message.get("sender_id"),
            len(text),
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
            logger.info("命令已匹配，直接返回命令卡片：消息=%s", message_id)
            self._reply_card(message, command_response.card)
            return

        reaction_id = self._add_processing_reaction(message_id)
        try:
            conversation = self._conversation_for(message)
        except ConfigError as exc:
            logger.exception("选择 AI 会话配置失败：消息=%s", message_id)
            self._reply_card(message, cards.build_error_card("会话配置错误", str(exc)))
            self._delete_reaction(message_id, reaction_id)
            return
        if not conversation:
            self._reply_card(
                message,
                cards.build_error_card(
                    "话题会话未找到",
                    "当前话题未关联到机器人回复，无法安全续接会话。请在机器人回复卡片下创建话题。",
                ),
            )
            self._delete_reaction(message_id, reaction_id)
            return

        try:
            prompt_result = build_prompt(message, self.messenger, self.config, resume=conversation.resume)
        except FeishuApiError as exc:
            logger.exception("构建 AI 提示词失败：消息=%s", message_id)
            self._reply_card(message, cards.build_error_card("上下文构建失败", str(exc)))
            self._delete_reaction(message_id, reaction_id)
            return

        threading.Thread(
            target=self._run_ai_and_reply,
            args=(message, prompt_result.prompt, reaction_id, conversation),
            name=f"ai-{message_id}",
            daemon=True,
        ).start()

    def _run_ai_and_reply(
        self,
        message: dict[str, Any],
        prompt: dict[str, Any],
        reaction_id: str | None,
        conversation: Conversation,
    ) -> None:
        message_id = message["message_id"]
        try:
            logger.info(
                "开始 AI 回复：消息=%s 会话=%s 模式=%s CLI=%s 模型=%s",
                message_id,
                conversation.session_id,
                "续接" if conversation.resume else "新建",
                conversation.tool,
                conversation.model,
            )
            result = self.ai_runner.run(
                prompt,
                message_id,
                tool=conversation.tool,
                model=conversation.model,
                session_id=None if conversation.resume else conversation.session_id,
                resume_session_id=conversation.session_id if conversation.resume else None,
            )
            if result.ok:
                delivery = extract_file_deliveries(result.result, self.config.resolve_path("ai.workspace"))
                if delivery.paths:
                    self._reply_deliveries(message, delivery.paths, delivery.text, result, conversation)
                    return
                result_text = delivery.text
                if delivery.errors:
                    result_text = "\n".join([result_text, *[f"文件交付失败：{error}" for error in delivery.errors]]).strip()
                card = cards.build_ai_card(result.tool, result.model, result_text or "任务已完成。", result.thinking)
            else:
                card = cards.build_error_card("AI 分析失败", result.error or "未能获取有效输出", result.stderr)
            try:
                reply = self.messenger.reply_card(message_id, card, reply_in_thread=self._reply_in_thread(message))
            except FeishuApiError:
                logger.exception("发送 AI 回复卡片失败：消息=%s", message_id)
                return
            if result.ok and result.session_id:
                self._save_session_mappings(message, reply, conversation, result.session_id)
            logger.info("AI 回复卡片已发送：请求消息=%s 回复消息=%s", message_id, reply.get("message_id"))
        finally:
            self._delete_reaction(message_id, reaction_id)

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
            logger.info("解析到话题续接会话：消息=%s 会话=%s CLI=%s 模型=%s", message.get("message_id"), session_id, tool, model)
            return Conversation(session_id=session_id, tool=tool, model=model, resume=True)

        tool = self.config.current_tool()
        model = self.config.resolve_model(tool)
        session_id = str(uuid4()) if self.ai_runner.supports_explicit_session(tool) else None
        logger.info(
            "创建新 AI 会话：消息=%s 会话=%s CLI=%s 模型=%s",
            message.get("message_id"),
            session_id or "由 CLI 输出确定",
            tool,
            model,
        )
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

    def _add_processing_reaction(self, message_id: str) -> str | None:
        emoji = self.config.get("feishu.reaction_processing", "")
        if not emoji:
            return None
        try:
            reaction_id = self.messenger.add_reaction(message_id, emoji)
            logger.info("已添加 OnIt 表情：消息=%s 表情=%s 关联=%s", message_id, emoji, reaction_id or "（未返回 ID）")
            return reaction_id
        except FeishuApiError as exc:
            logger.warning("添加 OnIt 表情失败：消息=%s 错误=%s", message_id, exc)
            return None

    def _delete_reaction(self, message_id: str, reaction_id: str | None) -> None:
        if not reaction_id:
            return
        try:
            self.messenger.delete_reaction(message_id, reaction_id)
            logger.info("已移除 OnIt 表情：消息=%s 表情=%s", message_id, reaction_id)
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
