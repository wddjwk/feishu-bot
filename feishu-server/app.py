from __future__ import annotations

import json
from typing import Any

from config import Config
from handlers.message_handler import MessageHandler
import logger


class FeishuBotApp:
    def __init__(self, app_id: str, app_secret: str, config: Config, message_handler: MessageHandler) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.config = config
        self.message_handler = message_handler

    def start(self) -> None:
        import lark_oapi as lark

        logger.reconfigure_lark()
        event_handler = self._build_event_handler(lark)
        log_level = getattr(lark.LogLevel, str(self.config.get("logging.level", "INFO")).upper(), lark.LogLevel.INFO)
        client = lark.ws.Client(self.app_id, self.app_secret, event_handler=event_handler, log_level=log_level)
        logger.info("正在启动飞书长连接客户端")
        client.start()

    def _build_event_handler(self, lark: Any) -> Any:
        builder = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(self._on_message_receive)
        for method_name in (
            "register_p2_im_chat_access_event_bot_p2p_chat_entered_v1",
            "register_p2_im_message_message_read_v1",
            "register_p2_im_message_reaction_created_v1",
            "register_p2_im_message_reaction_deleted_v1",
        ):
            register = getattr(builder, method_name, None)
            if register:
                builder = register(self._on_ignored_event)
        builder = builder.register_p1_customized_event("p2p_chat_create", self._on_ignored_event)
        return builder.build()

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
        logger.debug("已忽略飞书事件：类型=%s", event_type)
