import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FakeConfig:
    def version(self):
        return "v1.2.3"


class LoggingConfig:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir

    def resolve_path(self, dotted):
        if dotted != "logging.dir":
            raise AssertionError(dotted)
        return self.log_dir


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_card(self, receive_id, receive_id_type, card):
        self.sent.append({"receive_id": receive_id, "receive_id_type": receive_id_type, "card": card})


class ImmediateTimer:
    def __init__(self, _delay, callback):
        self.callback = callback

    def start(self):
        self.callback()


class StartupNotificationTests(unittest.TestCase):
    def test_startup_without_maintainer_does_not_send_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending_restart.json"
            messenger = FakeMessenger()
            with patch.object(main, "PENDING_RESTART_PATH", pending), patch.dict(main.os.environ, {}, clear=True):
                main.send_startup_notifications(FakeConfig(), messenger)

        self.assertEqual(messenger.sent, [])

    def test_pending_restart_is_merged_into_single_startup_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending_restart.json"
            pending.write_text(json.dumps({"receive_id": "ou_ignored", "reply_text": "更新完成"}), encoding="utf-8")
            messenger = FakeMessenger()
            with (
                patch.object(main, "PENDING_RESTART_PATH", pending),
                patch.object(main.threading, "Timer", ImmediateTimer),
                patch.dict(main.os.environ, {"MAINTAINER_OPEN_ID": "ou_abcdefgh"}, clear=True),
            ):
                main.send_startup_notifications(FakeConfig(), messenger)

        self.assertEqual(len(messenger.sent), 1)
        self.assertEqual(messenger.sent[0]["receive_id"], "ou_abcdefgh")
        content = messenger.sent[0]["card"]["body"]["elements"][0]["content"]
        self.assertIn("当前版本：`v1.2.3`", content)
        self.assertIn("更新完成", content)

    def test_private_rotating_log_handler_creates_owner_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bot.log"
            handler = main.PrivateRotatingFileHandler(path, maxBytes=1024, backupCount=1, encoding="utf-8")
            handler.close()

            mode = stat.S_IMODE(path.stat().st_mode)

        self.assertEqual(mode, 0o600)

    def test_logging_uses_current_process_pid_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main.os, "getpid", return_value=4242):
            path = main._current_log_path(LoggingConfig(Path(tmp)))

        self.assertEqual(path, Path(tmp) / "feishu-bot-4242.log")


if __name__ == "__main__":
    unittest.main()
