from __future__ import annotations

import json
import os
import re
import shutil
import signal
import threading
from pathlib import Path

import cards
from app import FeishuBotApp
from config import DEFAULT_USER_CONFIG_PATH, Config
from handlers.message_handler import MessageHandler
from services.ai_runner import AIRunner
from services.messenger import FeishuApiError, Messenger, TenantTokenProvider
from services.scheduler import Scheduler
from services.session_store import SessionStore
import logger


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
    log_dir = config.resolve_path("server.logging.dir")
    level_name = str(config.get("server.logging.level", "INFO")).upper()
    file_max_bytes = int(config.get("server.logging.file_max_bytes", 100 * 1024 * 1024))
    backup_count = int(config.get("server.logging.backup_count", 5))
    logger.setup_logging(log_dir, level=level_name, max_bytes=file_max_bytes, backup_count=backup_count)


def cli_availability(config: Config) -> list[tuple[str, str, bool]]:
    tools = config.get("ai.tools", {})
    if not isinstance(tools, dict):
        return []
    results: list[tuple[str, str, bool]] = []
    for name, cfg in tools.items():
        if not isinstance(cfg, dict):
            continue
        command = str(cfg.get("command") or name)
        results.append((name, command, bool(shutil.which(command))))
    return results


def apply_default_tool_fallback(config: Config) -> list[tuple[str, str, bool]]:
    results = cli_availability(config)
    default = config.default_tool()
    if any(name == default and exists for name, _, exists in results):
        return results
    fallback = next((name for name, _, exists in results if exists), None)
    if fallback:
        config.set_tool(fallback)
        logger.warning("默认 CLI %s 未安装，已回退到 %s", default, fallback)
    else:
        logger.error("默认 CLI %s 未安装，且没有其他已安装的 AI CLI 可用", default)
    return results


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

    receive_id = os.getenv("MAINTAINER_OPEN_ID", "").strip()
    if not _looks_like_open_id(receive_id):
        logger.info("未发送启动通知：MAINTAINER_OPEN_ID 未配置或格式无效")
        return

    tool = config.current_tool()
    model = config.resolve_model(tool)
    lines = [
        "服务器已启动。",
        "",
        f"当前版本：`{config.version()}`",
        "",
        f"当前 CLI：`{tool}` / `{model}`",
        "",
        "CLI 可用性：",
    ]
    for name, command, exists in cli_availability(config):
        status = "已安装" if exists else "未安装"
        lines.append(f"- `{name}`（{command}）：{status}")
    if pending_data.get("reply_text"):
        lines.extend(["", pending_data["reply_text"]])
    card = cards.build_generic_card("FeishuBot 已启动", "\n".join(lines), "green")
    threading.Timer(3.0, lambda: _safe_send_card(messenger, receive_id, "open_id", card)).start()


def _looks_like_open_id(value: str) -> bool:
    return bool(re.match(r"^ou_[A-Za-z0-9_-]{8,}$", value)) and value != "ou_xxx"


def _safe_send_card(messenger: Messenger, receive_id: str, receive_id_type: str, card: dict) -> None:
    try:
        messenger.send_card(receive_id, receive_id_type, card)
    except FeishuApiError:
        logger.exception("发送启动通知失败")


def main() -> None:
    load_env()
    config = Config(user_path=DEFAULT_USER_CONFIG_PATH)
    setup_logging(config)
    apply_default_tool_fallback(config)
    try:
        cards.configure_card_width(str(config.get("options.card_width", "half")))
        cards.configure_tool_icons(config.tool_icons())
    except ValueError as exc:
        logger.error("卡片宽度配置无效：%s", exc)
        raise SystemExit(2) from exc
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        logger.error(".env 中必须配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        raise SystemExit(2)

    token_provider = TenantTokenProvider(app_id, app_secret, str(config.get("server.base_url")))
    messenger = Messenger(config, token_provider)
    ai_runner = AIRunner(config)
    session_store = SessionStore(config)
    scheduler = Scheduler(config, messenger, ai_runner)
    bot_open_id = os.getenv("FEISHU_BOT_OPEN_ID", "").strip()
    if not _looks_like_open_id(bot_open_id):
        bot_open_id = ""
    message_handler = MessageHandler(config, messenger, ai_runner, session_store, scheduler, bot_open_id=bot_open_id)
    app = FeishuBotApp(app_id, app_secret, config, message_handler)

    default_tool = config.current_tool()
    default_model = config.resolve_model(default_tool)
    logger.info(
        "服务初始化完成：版本=%s 默认CLI=%s 默认模型=%s 工具列表=%s",
        config.version(),
        default_tool,
        default_model,
        ",".join(config.tools()),
    )

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
