import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class FakeConfig:
    def version(self):
        return "v1.2.3"


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
    def test_pending_restart_is_merged_into_single_startup_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "pending_restart.json"
            pending.write_text(json.dumps({"receive_id": "ou_user", "reply_text": "更新完成"}), encoding="utf-8")
            messenger = FakeMessenger()
            with patch.object(main, "PENDING_RESTART_PATH", pending), patch.object(
                main, "resolve_maintainer_open_id", return_value="ou_admin"
            ), patch.object(main.threading, "Timer", ImmediateTimer):
                main.send_startup_notifications(FakeConfig(), messenger)

        self.assertEqual(len(messenger.sent), 1)
        self.assertEqual(messenger.sent[0]["receive_id"], "ou_user")
        content = messenger.sent[0]["card"]["body"]["elements"][0]["content"]
        self.assertIn("当前版本：`v1.2.3`", content)
        self.assertIn("更新完成", content)


if __name__ == "__main__":
    unittest.main()
