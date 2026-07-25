import unittest
from pathlib import Path
from unittest.mock import patch

from services.messenger import Messenger
from utils.lark_cli_wrapper import LarkCliError


class FakeConfig:
    def get(self, dotted: str, default=None):
        if dotted == "server.base_url":
            return "https://open.feishu.cn/open-apis"
        return default


class FakeLarkCli:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def download_message_resource(self, message_id, file_key, resource_type, dest):
        self.calls.append(("download", message_id, file_key, resource_type, dest))
        return dest

    def reply_file(self, message_id, path, *, reply_in_thread):
        self.calls.append(("reply_file", message_id, path, reply_in_thread))
        return {"message_id": "om_file"}

    def reply_image(self, message_id, path, *, reply_in_thread):
        self.calls.append(("reply_image", message_id, path, reply_in_thread))
        return {"message_id": "om_image"}

    def send_file(self, chat_id, path):
        self.calls.append(("send_file", chat_id, path))
        return {"message_id": "om_file"}

    def send_image(self, chat_id, path):
        self.calls.append(("send_image", chat_id, path))
        return {"message_id": "om_image"}


class MessengerTests(unittest.TestCase):
    def test_bot_open_id_is_loaded_once_and_cached(self):
        messenger = Messenger(FakeConfig(), token_provider=None, lark_cli=FakeLarkCli())
        with patch.object(messenger, "_request_json", return_value={"bot": {"open_id": "ou_bot"}}) as request:
            self.assertEqual(messenger.get_bot_open_id(), "ou_bot")
            self.assertEqual(messenger.get_bot_open_id(), "ou_bot")

        request.assert_called_once_with("GET", "/bot/v3/info")

    def test_resource_download_prefers_lark_cli(self):
        cli = FakeLarkCli()
        messenger = Messenger(FakeConfig(), token_provider=None, lark_cli=cli)
        path = messenger.download_message_resource("om_1", "img_1", "image", Path("download/image.png"))

        self.assertEqual(path, Path("download/image.png"))
        self.assertEqual(cli.calls[0][:4], ("download", "om_1", "img_1", "image"))

    def test_resource_download_falls_back_to_api_when_lark_cli_fails(self):
        class FailingCli(FakeLarkCli):
            def download_message_resource(self, *_args):
                raise LarkCliError("cli failed")

        messenger = Messenger(FakeConfig(), token_provider=None, lark_cli=FailingCli())
        with patch.object(messenger, "_download_message_resource_via_api", return_value=Path("download/fallback.png")) as fallback:
            path = messenger.download_message_resource("om_1", "img_1", "image", Path("download/image.png"))

        self.assertEqual(path, Path("download/fallback.png"))
        fallback.assert_called_once()


if __name__ == "__main__":
    unittest.main()
