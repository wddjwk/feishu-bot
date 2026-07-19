from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

import cards
from app import FeishuBotApp
from config import Config
from handlers.message_handler import MessageHandler
from services.ai_runner import AIRunner
from services.messenger import FeishuApiError, Messenger, TenantTokenProvider
from services.scheduler import Scheduler
from services.session_store import SessionStore


BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
PENDING_RESTART_PATH = ROOT_DIR / "pending_restart.json"


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT_DIR / ".env")


def setup_logging(config: Config) -> None:
    log_dir = config.resolve_path("logging.dir")
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_dir.chmod(0o700)
    log_file = log_dir / "feishu-bot.log"
    level_name = str(config.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    file_max_bytes = int(config.get("logging.file_max_bytes", 10 * 1024 * 1024))
    backup_count = int(config.get("logging.backup_count", 10))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[PrivateRotatingFileHandler(log_file, maxBytes=file_max_bytes, backupCount=backup_count, encoding="utf-8")],
    )
    logging.getLogger("lark_oapi").setLevel(level)
    logging.getLogger(__name__).info("日志已初始化：文件=%s 单文件上限=%s 字节 保留备份=%s", log_file, file_max_bytes, backup_count)


def send_startup_notifications(config: Config, messenger: Messenger) -> None:
    pending_data: dict[str, str] = {}
    if PENDING_RESTART_PATH.exists():
        try:
            loaded = json.loads(PENDING_RESTART_PATH.read_text(encoding="utf-8"))
            pending_data = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            PENDING_RESTART_PATH.unlink(missing_ok=True)
            pending_data = {}
        PENDING_RESTART_PATH.unlink(missing_ok=True)

    receive_id = pending_data.get("receive_id")
    receive_id_type = pending_data.get("receive_id_type") or "open_id"
    if not receive_id:
        return

    lines = [f"服务器已启动。", "", f"当前版本：`{config.version()}`"]
    if pending_data.get("reply_text"):
        lines.extend(["", pending_data["reply_text"]])
    card = cards.build_generic_card("FeishuBot 已启动", "\n".join(lines), "green")
    threading.Timer(3.0, lambda: _safe_send_card(messenger, receive_id, receive_id_type, card)).start()


def _looks_like_open_id(value: str) -> bool:
    return bool(re.match(r"^ou_[A-Za-z0-9_-]{8,}$", value)) and value != "ou_xxx"


def _safe_send_card(messenger: Messenger, receive_id: str, receive_id_type: str, card: dict) -> None:
    try:
        messenger.send_card(receive_id, receive_id_type, card)
    except FeishuApiError:
        logging.getLogger(__name__).exception("发送启动通知失败")


def main() -> None:
    load_env()
    config = Config()
    setup_logging(config)
    logger = logging.getLogger(__name__)
    try:
        cards.configure_card_width(str(config.get("feishu.card_width", "half")))
        cards.configure_tool_icons(config.get("feishu.tool_icons", {}))
    except ValueError as exc:
        logger.error("卡片宽度配置无效：%s", exc)
        raise SystemExit(2) from exc
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        logger.error(".env 中必须配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        raise SystemExit(2)

    token_provider = TenantTokenProvider(app_id, app_secret, str(config.get("feishu.base_url")))
    messenger = Messenger(config, token_provider)
    ai_runner = AIRunner(config)
    session_store = SessionStore(config)
    scheduler = Scheduler(config, messenger, ai_runner)
    bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
    if not _looks_like_open_id(bot_open_id):
        bot_open_id = ""
    message_handler = MessageHandler(config, messenger, ai_runner, session_store, scheduler, bot_open_id=bot_open_id)
    app = FeishuBotApp(app_id, app_secret, config, message_handler)

    def stop(signum: int, _frame: object) -> None:
        logger.info("收到退出信号 %s，正在停止调度器", signum)
        scheduler.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler.start()
    send_startup_notifications(config, messenger)
    app.start()


if __name__ == "__main__":
    main()
