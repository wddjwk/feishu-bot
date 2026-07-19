from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from handlers.message_handler import MessageHandler
from services.ai_runner import AIResult
from utils.lark_cli_wrapper import LarkCliError


class FakeConfig:
    def __init__(self) -> None:
        self.download_dir = Path(tempfile.mkdtemp())

    def get(self, dotted: str, default=None):
        values = {
            "feishu.reaction_processing": "OnIt",
            "prompt.reply_chain_limit": 20,
            "prompt.thread_message_limit": 50,
        }
        return values.get(dotted, default)

    def resolve_path(self, dotted: str) -> Path:
        return self.download_dir

    def current_tool(self) -> str:
        return "claude"

    def resolve_model(self, tool: str | None = None, requested: str | None = None) -> str:
        return "deepseek-v4-flash"


class FakeMessenger:
    def __init__(self) -> None:
        self.replies: list[dict] = []
        self.reactions: list[dict] = []
        self.deleted_reactions: list[dict] = []
        self.files: list[dict] = []
        self.bot_info_calls = 0
        self.bot_open_id = "ou_bot"

    def reply_card(self, message_id: str, card: dict, *, reply_in_thread: bool | None = None):
        self.replies.append({"message_id": message_id, "reply_in_thread": reply_in_thread, "card": card})
        return {"message_id": f"bot-{message_id}", "thread_id": "thread-1" if reply_in_thread else ""}

    def get_message(self, message_id: str):
        return {
            "message_id": message_id,
            "message_type": "text",
            "content": json.dumps({"text": "previous"}),
            "create_time": "1",
        }

    def list_messages(self, *_args, **_kwargs):
        return []

    def add_reaction(self, message_id: str, emoji_type: str):
        reaction_id = f"reaction-{len(self.reactions) + 1}"
        self.reactions.append({"message_id": message_id, "emoji_type": emoji_type, "reaction_id": reaction_id})
        return reaction_id

    def delete_reaction(self, message_id: str, reaction_id: str):
        self.deleted_reactions.append({"message_id": message_id, "reaction_id": reaction_id})

    def get_bot_open_id(self):
        self.bot_info_calls += 1
        return self.bot_open_id

    def download_message_resource(self, _message_id, _file_key, _resource_type, dest: Path):
        return dest

    def reply_file(self, message_id: str, path: Path, *, reply_in_thread: bool):
        self.files.append({"kind": "file", "message_id": message_id, "path": path, "reply_in_thread": reply_in_thread})
        return {"message_id": f"file-{message_id}", "thread_id": "thread-1" if reply_in_thread else ""}

    def reply_image(self, message_id: str, path: Path, *, reply_in_thread: bool):
        self.files.append({"kind": "image", "message_id": message_id, "path": path, "reply_in_thread": reply_in_thread})
        return {"message_id": f"image-{message_id}", "thread_id": "thread-1" if reply_in_thread else ""}


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, prompt, message_id, *, tool=None, model=None, session_id=None, resume_session_id=None):
        self.calls.append(
            {
                "prompt": prompt,
                "message_id": message_id,
                "tool": tool,
                "model": model,
                "session_id": session_id,
                "resume_session_id": resume_session_id,
            }
        )
        return AIResult(
            True,
            tool or "claude",
            model or "deepseek-v4-flash",
            result="done",
            session_id=session_id or resume_session_id,
        )

    def supports_explicit_session(self, _tool=None):
        return True


class FakeSessionStore:
    def __init__(self, resume_info: dict | None = None) -> None:
        self.saved: list[dict] = []
        self.resume_info = resume_info

    def resolve_for_thread(self, message: dict):
        return self.resume_info if message.get("thread_id") else None

    def set(self, key: str, session_id: str, *, tool: str, model: str, link_type="message", metadata=None) -> None:
        self.saved.append(
            {
                "key": key,
                "session_id": session_id,
                "tool": tool,
                "model": model,
                "link_type": link_type,
                "metadata": metadata,
            }
        )


class ImmediateThread:
    def __init__(self, *, target, args=(), kwargs=None, **_unused):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


def message_event(message: dict) -> dict:
    return {
        "event": {
            "message": message,
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
        }
    }


class MessageHandlerFlowTests(unittest.TestCase):
    def test_normal_message_starts_explicit_session_and_cleans_processing_reaction(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        store = FakeSessionStore()
        handler = MessageHandler(FakeConfig(), messenger, runner, store, scheduler=None)
        event = message_event(
            {
                "message_id": "m1",
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
                "parent_id": "bot-old",
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread), patch(
            "handlers.message_handler.uuid4", return_value=UUID("12345678-1234-5678-1234-567812345678")
        ):
            handler.handle_event(event)

        self.assertIsNone(runner.calls[0]["resume_session_id"])
        self.assertEqual(runner.calls[0]["session_id"], "12345678-1234-5678-1234-567812345678")
        self.assertEqual(runner.calls[0]["tool"], "claude")
        self.assertEqual(runner.calls[0]["model"], "deepseek-v4-flash")
        self.assertFalse(messenger.replies[0]["reply_in_thread"])
        self.assertEqual({item["key"] for item in store.saved}, {"m1", "bot-m1"})
        self.assertEqual({item["session_id"] for item in store.saved}, {"12345678-1234-5678-1234-567812345678"})
        self.assertEqual({item["link_type"] for item in store.saved}, {"user_message", "bot_reply"})
        self.assertEqual(messenger.reactions[0]["emoji_type"], "OnIt")
        self.assertEqual(messenger.deleted_reactions, [{"message_id": "m1", "reaction_id": "reaction-1"}])

    def test_thread_followup_resumes_original_cli_model_and_cleans_processing_reaction(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        store = FakeSessionStore({"session_id": "old-session", "tool": "qoder", "model": "DeepSeek-V4-Flash"})
        handler = MessageHandler(FakeConfig(), messenger, runner, store, scheduler=None)
        event = message_event(
            {
                "message_id": "m2",
                "message_type": "text",
                "content": json.dumps({"text": "continue"}),
                "thread_id": "thread-1",
                "root_id": "bot-root",
                "create_time": "3",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(runner.calls[0]["resume_session_id"], "old-session")
        self.assertIsNone(runner.calls[0]["session_id"])
        self.assertEqual(runner.calls[0]["tool"], "qoder")
        self.assertEqual(runner.calls[0]["model"], "DeepSeek-V4-Flash")
        self.assertTrue(messenger.replies[0]["reply_in_thread"])
        self.assertEqual({item["key"] for item in store.saved}, {"m2", "bot-m2", "thread-1"})
        self.assertEqual({item["session_id"] for item in store.saved}, {"old-session"})
        self.assertEqual({item["link_type"] for item in store.saved}, {"user_message", "bot_reply", "thread"})
        self.assertEqual(messenger.deleted_reactions, [{"message_id": "m2", "reaction_id": "reaction-1"}])

    def test_standalone_file_message_is_ignored(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None)
        event = message_event(
            {
                "message_id": "m-file",
                "message_type": "file",
                "content": json.dumps({"file_key": "file-key", "file_name": "notes.txt"}),
                "create_time": "2",
            }
        )

        handler.handle_event(event)

        self.assertEqual(runner.calls, [])
        self.assertEqual(messenger.replies, [])
        self.assertEqual(messenger.reactions, [])

    def test_reply_to_file_downloads_it_and_starts_an_agent(self):
        messenger = FakeMessenger()
        messenger.get_message = lambda message_id: {
            "message_id": message_id,
            "message_type": "file",
            "content": json.dumps({"file_key": "file-key", "file_name": "notes.txt"}),
            "create_time": "1",
        }
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None)
        event = message_event(
            {
                "message_id": "m-reply-file",
                "message_type": "text",
                "parent_id": "m-file",
                "content": json.dumps({"text": "请分析这个文件"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(len(runner.calls), 1)
        attached = runner.calls[0]["prompt"]["context"]["attached_files"]
        self.assertEqual(attached[0]["key"], "file-key")
        self.assertIn("@", "\n".join(runner.calls[0]["prompt"]["context"]["conversation_history"]))

    def test_group_reply_to_file_requires_and_honors_bot_mention(self):
        messenger = FakeMessenger()
        messenger.get_message = lambda message_id: {
            "message_id": message_id,
            "message_type": "file",
            "content": json.dumps({"file_key": "file-key", "file_name": "notes.txt"}),
            "create_time": "1",
        }
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None, bot_open_id="ou_bot")
        event = message_event(
            {
                "message_id": "m-group-reply-file",
                "message_type": "text",
                "chat_type": "group",
                "parent_id": "m-file",
                "mentions": [{"id": {"open_id": "ou_bot"}, "mentioned_type": "bot"}],
                "content": json.dumps({"text": "@_user_1 请分析"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["prompt"]["context"]["attached_files"][0]["key"], "file-key")

    def test_generated_file_is_sent_before_completion_card(self):
        class FileRunner(FakeRunner):
            def __init__(self, path: Path) -> None:
                super().__init__()
                self.path = path

            def run(self, prompt, message_id, *, tool=None, model=None, session_id=None, resume_session_id=None):
                self.calls.append({"prompt": prompt, "message_id": message_id, "session_id": session_id})
                return AIResult(
                    True,
                    tool or "claude",
                    model or "deepseek-v4-flash",
                    result=f"文件已完成。\n[[FEISHU_FILE:{self.path}]]",
                    session_id=session_id or resume_session_id,
                )

        messenger = FakeMessenger()
        config = FakeConfig()
        output = config.download_dir / "report.txt"
        output.write_text("done", encoding="utf-8")
        runner = FileRunner(output)
        store = FakeSessionStore()
        handler = MessageHandler(config, messenger, runner, store, scheduler=None)
        event = message_event(
            {
                "message_id": "m-delivery",
                "message_type": "text",
                "content": json.dumps({"text": "生成报告"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(messenger.files[0]["kind"], "file")
        self.assertEqual(messenger.files[0]["message_id"], "m-delivery")
        self.assertEqual(messenger.replies[0]["message_id"], "file-m-delivery")
        self.assertIn("文件已完成", messenger.replies[0]["card"]["body"]["elements"][0]["content"])
        self.assertIn("bot_file", {item["link_type"] for item in store.saved})

    def test_generated_image_falls_back_to_file_when_image_send_fails(self):
        class ImageFailingMessenger(FakeMessenger):
            def reply_image(self, *_args, **_kwargs):
                raise LarkCliError("image upload rejected")

        class ImageRunner(FakeRunner):
            def __init__(self, path: Path) -> None:
                super().__init__()
                self.path = path

            def run(self, _prompt, _message_id, *, tool=None, model=None, session_id=None, resume_session_id=None):
                return AIResult(
                    True,
                    tool or "claude",
                    model or "deepseek-v4-flash",
                    result=f"图片已完成。\n[[FEISHU_FILE:{self.path}]]",
                    session_id=session_id or resume_session_id,
                )

        config = FakeConfig()
        image = config.download_dir / "report.png"
        image.write_bytes(b"image")
        messenger = ImageFailingMessenger()
        handler = MessageHandler(config, messenger, ImageRunner(image), FakeSessionStore(), scheduler=None)
        event = message_event(
            {
                "message_id": "m-image-delivery",
                "message_type": "text",
                "content": json.dumps({"text": "生成图片"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(messenger.files[0]["kind"], "file")

    def test_failed_new_run_does_not_create_resumable_mapping(self):
        class FailingRunner(FakeRunner):
            def run(self, prompt, message_id, *, tool=None, model=None, session_id=None, resume_session_id=None):
                self.calls.append({"prompt": prompt, "message_id": message_id, "session_id": session_id})
                return AIResult(False, tool or "claude", model or "deepseek-v4-flash", error="启动失败", session_id=session_id)

        messenger = FakeMessenger()
        runner = FailingRunner()
        store = FakeSessionStore()
        handler = MessageHandler(FakeConfig(), messenger, runner, store, scheduler=None)
        event = message_event(
            {
                "message_id": "m-failed",
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(store.saved, [])
        self.assertEqual(messenger.replies[0]["card"]["header"]["title"]["content"], "⚠️ AI 分析失败")

    def test_tool_without_explicit_session_uses_cli_reported_session_for_mapping(self):
        class LegacyRunner(FakeRunner):
            def supports_explicit_session(self, _tool=None):
                return False

            def run(self, prompt, message_id, *, tool=None, model=None, session_id=None, resume_session_id=None):
                self.calls.append({"prompt": prompt, "message_id": message_id, "session_id": session_id})
                return AIResult(True, tool or "claude", model or "deepseek-v4-flash", result="done", session_id="native-session")

        messenger = FakeMessenger()
        runner = LegacyRunner()
        store = FakeSessionStore()
        handler = MessageHandler(FakeConfig(), messenger, runner, store, scheduler=None)
        event = message_event(
            {
                "message_id": "m-legacy",
                "message_type": "text",
                "content": json.dumps({"text": "hi"}),
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertIsNone(runner.calls[0]["session_id"])
        self.assertEqual({item["session_id"] for item in store.saved}, {"native-session"})

    def test_group_message_without_bot_mention_is_ignored(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None, bot_open_id="ou_bot")
        event = message_event(
            {
                "message_id": "m-group",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "chat_type": "group",
                "mentions": [],
                "create_time": "2",
            }
        )

        handler.handle_event(event)

        self.assertEqual(runner.calls, [])
        self.assertEqual(messenger.replies, [])
        self.assertEqual(messenger.reactions, [])

    def test_group_message_mentioning_another_bot_is_ignored(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None, bot_open_id="ou_bot")
        event = message_event(
            {
                "message_id": "m-other-bot",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 hello"}),
                "chat_type": "group",
                "mentions": [{"id": {"open_id": "ou_other_bot"}, "mentioned_type": "bot"}],
                "create_time": "2",
            }
        )

        handler.handle_event(event)

        self.assertEqual(runner.calls, [])
        self.assertEqual(messenger.replies, [])

    def test_group_bot_mention_uses_bot_info_fallback_and_starts_agent(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None)
        event = message_event(
            {
                "message_id": "m-mentioned",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 hello"}),
                "chat_type": "group",
                "mentions": [{"id": {"open_id": "ou_bot"}, "mentioned_type": "bot"}],
                "create_time": "2",
            }
        )

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertEqual(messenger.bot_info_calls, 1)
        self.assertEqual(len(runner.calls), 1)

    def test_group_topic_without_bot_mention_is_ignored(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        store = FakeSessionStore({"session_id": "old-session", "tool": "qoder", "model": "DeepSeek-V4-Flash"})
        handler = MessageHandler(FakeConfig(), messenger, runner, store, scheduler=None, bot_open_id="ou_bot")
        event = message_event(
            {
                "message_id": "m-topic-group",
                "message_type": "text",
                "content": json.dumps({"text": "continue"}),
                "chat_type": "group",
                "thread_id": "thread-1",
                "root_id": "bot-root",
                "mentions": [],
                "create_time": "2",
            }
        )

        handler.handle_event(event)

        self.assertEqual(runner.calls, [])
        self.assertEqual(messenger.replies, [])

    def test_unmapped_topic_returns_error_without_starting_new_session(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        handler = MessageHandler(FakeConfig(), messenger, runner, FakeSessionStore(), scheduler=None)
        event = message_event(
            {
                "message_id": "m-unmapped",
                "message_type": "text",
                "content": json.dumps({"text": "continue"}),
                "thread_id": "thread-unknown",
                "root_id": "bot-root",
                "create_time": "2",
            }
        )

        handler.handle_event(event)

        self.assertEqual(runner.calls, [])
        self.assertTrue(messenger.replies[0]["reply_in_thread"])
        self.assertEqual(messenger.replies[0]["card"]["header"]["title"]["content"], "⚠️ 话题会话未找到")
        self.assertEqual(messenger.deleted_reactions, [{"message_id": "m-unmapped", "reaction_id": "reaction-1"}])


if __name__ == "__main__":
    unittest.main()
