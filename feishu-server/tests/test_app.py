import unittest

from app import FeishuBotApp


class FakeConfig:
    def get(self, _dotted, default=None):
        return default


class FakeBuilder:
    def __init__(self):
        self.registered: list[str] = []

    def _register(self, name, _handler):
        self.registered.append(name)
        return self

    def register_p2_im_message_receive_v1(self, handler):
        return self._register("receive", handler)

    def register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self, handler):
        return self._register("p2p_entered", handler)

    def register_p2_im_message_message_read_v1(self, handler):
        return self._register("message_read", handler)

    def register_p2_im_message_reaction_created_v1(self, handler):
        return self._register("reaction_created", handler)

    def register_p2_im_message_reaction_deleted_v1(self, handler):
        return self._register("reaction_deleted", handler)

    def register_p1_customized_event(self, _name, handler):
        return self._register("customized", handler)

    def build(self):
        return self


class FakeLark:
    class EventDispatcherHandler:
        @staticmethod
        def builder(_verification_token, _encrypt_key):
            return FakeBuilder()


class AppTests(unittest.TestCase):
    def test_registers_ignored_reaction_event_handlers(self):
        app = FeishuBotApp("cli_test", "secret", FakeConfig(), message_handler=object())

        handler = app._build_event_handler(FakeLark)

        self.assertIn("reaction_created", handler.registered)
        self.assertIn("reaction_deleted", handler.registered)


if __name__ == "__main__":
    unittest.main()
