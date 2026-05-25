from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

import cards
import commands
from config import Config
from handlers import BaseHandler
from services.ai_runner import AIRunner
from services.messenger import FeishuApiError, Messenger
from services.prompt_builder import build_prompt, extract_text, normalize_event
from services.session_store import SessionStore


logger = logging.getLogger(__name__)


class MessageHandler(BaseHandler):
    def __init__(
        self,
        config: Config,
        messenger: Messenger,
        ai_runner: AIRunner,
        session_store: SessionStore,
        scheduler: object,
    ) -> None:
        self.config = config
        self.messenger = messenger
        self.ai_runner = ai_runner
        self.session_store = session_store
        self.scheduler = scheduler
        self._seen: deque[str] = deque(maxlen=1000)
        self._seen_set: set[str] = set()

    def handle_event(self, event: dict[str, Any]) -> None:
        message = normalize_event(event)
        message_id = message.get("message_id")
        if not message_id:
            logger.warning("received message event without message_id")
            return
        if self._is_duplicate(message_id):
            logger.info("skip duplicate message_id=%s", message_id)
            return
        if message.get("sender_type") in {"bot", "app"}:
            logger.info("skip bot/app message_id=%s", message_id)
            return

        text = extract_text(message).strip()
        logger.info(
            "received message_id=%s type=%s thread_id=%s root_id=%s parent_id=%s sender_type=%s sender_id=%s text_len=%s",
            message_id,
            message.get("message_type"),
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
            self._reply_card(message, command_response.card)
            return

        resume_info = self.session_store.resolve_for_thread(message)
        resume_session_id = resume_info.get("session_id") if resume_info else None
        try:
            prompt_result = build_prompt(message, self.messenger, self.config, resume=bool(resume_session_id))
        except FeishuApiError as exc:
            logger.exception("failed to build prompt")
            self._reply_card(message, cards.build_error_card("上下文构建失败", str(exc)))
            return

        if prompt_result.file_only:
            logger.info("skip file-only message_id=%s", message_id)
            return

        reaction_id = self._add_processing_reaction(message_id)
        threading.Thread(
            target=self._run_ai_and_reply,
            args=(message, prompt_result.prompt, reaction_id, resume_info),
            name=f"ai-{message_id}",
            daemon=True,
        ).start()

    def _run_ai_and_reply(
        self,
        message: dict[str, Any],
        prompt: dict[str, Any],
        reaction_id: str | None,
        resume_info: dict[str, Any] | None,
    ) -> None:
        message_id = message["message_id"]
        tool = (resume_info or {}).get("tool") or self.config.current_tool()
        model = (resume_info or {}).get("model") or self.config.resolve_model(tool)
        result = self.ai_runner.run(
            prompt,
            message_id,
            tool=tool,
            model=model,
            resume_session_id=(resume_info or {}).get("session_id"),
        )
        if result.ok:
            card = cards.build_ai_card(result.tool, result.model, result.result, result.thinking)
        else:
            card = cards.build_error_card("AI 分析失败", result.error or "未能获取有效输出", result.stderr)
        try:
            reply = self.messenger.reply_card(message_id, card, reply_in_thread=self._reply_in_thread(message))
        except FeishuApiError as exc:
            logger.exception("failed to send AI reply message_id=%s", message_id)
            self._delete_reaction(message_id, reaction_id)
            return

        self._delete_reaction(message_id, reaction_id)
        self._add_done_reaction(message_id)
        if result.session_id:
            self._save_session_mappings(message, reply, result)

    def _save_session_mappings(self, user_message: dict[str, Any], reply: dict[str, Any], result: Any) -> None:
        metadata = {"source_message_id": user_message.get("message_id")}
        reply_id = reply.get("message_id")
        if reply_id:
            self.session_store.set(reply_id, result.session_id, tool=result.tool, model=result.model, metadata=metadata)
        if not user_message.get("thread_id"):
            return
        for key in (reply.get("thread_id"), user_message.get("thread_id")):
            if key:
                self.session_store.set(key, result.session_id, tool=result.tool, model=result.model, metadata=metadata)

    def _reply_card(self, message: dict[str, Any], card: dict[str, Any]) -> None:
        try:
            self.messenger.reply_card(message["message_id"], card, reply_in_thread=self._reply_in_thread(message))
        except FeishuApiError:
            logger.exception("failed to reply card message_id=%s", message.get("message_id"))

    @staticmethod
    def _reply_in_thread(message: dict[str, Any]) -> bool:
        return bool(message.get("thread_id"))

    def _add_processing_reaction(self, message_id: str) -> str | None:
        emoji = self.config.get("feishu.reaction_processing", "")
        if not emoji:
            return None
        try:
            return self.messenger.add_reaction(message_id, emoji)
        except FeishuApiError as exc:
            logger.warning("failed to add processing reaction message_id=%s: %s", message_id, exc)
            return None

    def _add_done_reaction(self, message_id: str) -> None:
        emoji = self.config.get("feishu.reaction_done", "")
        if not emoji:
            return
        try:
            self.messenger.add_reaction(message_id, emoji)
        except FeishuApiError as exc:
            logger.warning("failed to add done reaction message_id=%s: %s", message_id, exc)

    def _delete_reaction(self, message_id: str, reaction_id: str | None) -> None:
        if not reaction_id:
            return
        try:
            self.messenger.delete_reaction(message_id, reaction_id)
        except FeishuApiError as exc:
            logger.warning("failed to delete reaction message_id=%s reaction_id=%s: %s", message_id, reaction_id, exc)

    def _is_duplicate(self, message_id: str) -> bool:
        if message_id in self._seen_set:
            return True
        if len(self._seen) == self._seen.maxlen:
            old = self._seen.popleft()
            self._seen_set.discard(old)
        self._seen.append(message_id)
        self._seen_set.add(message_id)
        return False
