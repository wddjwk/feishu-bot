import unittest
from unittest.mock import patch

from services.messenger import Messenger


class FakeConfig:
    def get(self, dotted: str, default=None):
        if dotted == "feishu.base_url":
            return "https://open.feishu.cn/open-apis"
        return default


class MessengerTests(unittest.TestCase):
    def test_bot_open_id_is_loaded_once_and_cached(self):
        messenger = Messenger(FakeConfig(), token_provider=None)
        with patch.object(messenger, "_request_json", return_value={"bot": {"open_id": "ou_bot"}}) as request:
            self.assertEqual(messenger.get_bot_open_id(), "ou_bot")
            self.assertEqual(messenger.get_bot_open_id(), "ou_bot")

        request.assert_called_once_with("GET", "/bot/v3/info")


if __name__ == "__main__":
    unittest.main()
