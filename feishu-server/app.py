from __future__ import annotations

import json
import logging
from typing import Any

from config import Config
from handlers.message_handler import MessageHandler


logger = logging.getLogger(__name__)


class FeishuBotApp:
    def __init__(self, app_id: str, app_secret: str, config: Config, message_handler: MessageHandler) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.config = config
        self.message_handler = message_handler

    def start(self) -> None:
        import lark_oapi as lark

        builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message_receive)
        for method_name in (
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            "register_p2_im_message_message_read_v1",
        ):
            register = getattr(builder, method_name, None)
            if register:
                builder = register(self._on_ignored_event)
        builder = builder.register_p1_customized_event("p2p_chat_create", self._on_ignored_event)
        event_handler = builder.build()
        log_level = getattr(lark.LogLevel, str(self.config.get("logging.level", "INFO")).upper(), lark.LogLevel.INFO)
        client = lark.ws.Client(self.app_id, self.app_secret, event_handler=event_handler, log_level=log_level)
        logger.info("starting Feishu websocket client")
        client.start()

    def _on_message_receive(self, data: Any) -> None:
        import lark_oapi as lark

        raw = lark.JSON.marshal(data)
        event = json.loads(raw) if isinstance(raw, str) else raw
        self.message_handler.handle_event(event)

    def _on_ignored_event(self, data: Any) -> None:
        import lark_oapi as lark

        raw = lark.JSON.marshal(data)
        event = json.loads(raw) if isinstance(raw, str) else raw
        event_type = event.get("schema", "") or event.get("header", {}).get("event_type", "")
        logger.debug("ignored Feishu event type=%s", event_type)
