from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime
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


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT_DIR / ".env")


def setup_logging(config: Config) -> None:
    log_dir = config.resolve_path("logging.dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    level_name = str(config.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(log_file, encoding="utf-8")],
    )
    logging.getLogger("lark_oapi").setLevel(level)


def send_startup_notifications(config: Config, messenger: Messenger) -> None:
    logger = logging.getLogger(__name__)
    maintainer = resolve_maintainer_open_id(config, messenger)
    pending_data: dict[str, str] = {}
    if PENDING_RESTART_PATH.exists():
        try:
            loaded = json.loads(PENDING_RESTART_PATH.read_text(encoding="utf-8"))
            pending_data = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            PENDING_RESTART_PATH.unlink(missing_ok=True)
            pending_data = {}
        PENDING_RESTART_PATH.unlink(missing_ok=True)

    receive_id = pending_data.get("receive_id") or maintainer
    receive_id_type = pending_data.get("receive_id_type") or "open_id"
    if not receive_id:
        logger.warning("startup notification skipped: maintainer open_id is not configured or resolvable")
        return

    lines = [f"服务器已启动。", "", f"当前版本：`{config.version()}`"]
    if pending_data.get("reply_text"):
        lines.extend(["", pending_data["reply_text"]])
    card = cards.build_generic_card("FeishuBot 已启动", "\n".join(lines), "green")
    threading.Timer(3.0, lambda: _safe_send_card(messenger, receive_id, receive_id_type, card)).start()


def resolve_maintainer_open_id(config: Config, messenger: Messenger) -> str:
    open_id = os.getenv("FEISHU_MAINTAINER_OPEN_ID", "").strip()
    if _looks_like_open_id(open_id):
        return open_id
    name = os.getenv("FEISHU_MAINTAINER_NAME", "").strip() or str(config.get("feishu.maintainer_name", "")).strip()
    if not name:
        return ""
    try:
        resolved = messenger.find_user_open_id_by_name(name)
    except FeishuApiError as exc:
        logging.getLogger(__name__).warning("failed to resolve maintainer by name %s: %s", name, exc)
        return ""
    return resolved or ""


def _looks_like_open_id(value: str) -> bool:
    return bool(re.match(r"^ou_[A-Za-z0-9_-]{8,}$", value)) and value != "ou_xxx"


def _safe_send_card(messenger: Messenger, receive_id: str, receive_id_type: str, card: dict) -> None:
    try:
        messenger.send_card(receive_id, receive_id_type, card)
    except FeishuApiError:
        logging.getLogger(__name__).exception("failed to send notification")


def main() -> None:
    load_env()
    config = Config()
    setup_logging(config)
    logger = logging.getLogger(__name__)
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        logger.error("FEISHU_APP_ID and FEISHU_APP_SECRET are required in .env")
        raise SystemExit(2)

    token_provider = TenantTokenProvider(app_id, app_secret, str(config.get("feishu.base_url")))
    messenger = Messenger(config, token_provider)
    ai_runner = AIRunner(config)
    session_store = SessionStore(config)
    scheduler = Scheduler(config, messenger, ai_runner)
    message_handler = MessageHandler(config, messenger, ai_runner, session_store, scheduler)
    app = FeishuBotApp(app_id, app_secret, config, message_handler)

    def stop(signum: int, _frame: object) -> None:
        logger.info("received signal %s, stopping scheduler", signum)
        scheduler.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler.start()
    send_startup_notifications(config, messenger)
    app.start()


if __name__ == "__main__":
    main()
