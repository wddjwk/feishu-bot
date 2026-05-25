from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from handlers.message_handler import MessageHandler
from services.ai_runner import AIResult


class FakeConfig:
    def __init__(self) -> None:
        self.download_dir = Path(tempfile.mkdtemp())

    def get(self, dotted: str, default=None):
        values = {
            "feishu.reaction_processing": "",
            "feishu.reaction_done": "DONE",
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

    def add_reaction(self, *_args, **_kwargs):
        self.reactions.append({"args": _args, "kwargs": _kwargs})
        return None

    def delete_reaction(self, *_args, **_kwargs):
        return None


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, prompt, message_id, *, tool=None, model=None, resume_session_id=None):
        self.calls.append(
            {
                "prompt": prompt,
                "message_id": message_id,
                "tool": tool,
                "model": model,
                "resume_session_id": resume_session_id,
            }
        )
        return AIResult(True, tool or "claude", model or "deepseek-v4-flash", result="done", session_id="new-session")


class FakeSessionStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    def resolve_for_thread(self, message: dict):
        if message.get("thread_id"):
            return {"session_id": "old-session", "tool": "qoder", "model": "DeepSeek-V4-Flash"}
        return None

    def set(self, key: str, session_id: str, *, tool: str, model: str, metadata=None) -> None:
        self.saved.append({"key": key, "session_id": session_id, "tool": tool, "model": model, "metadata": metadata})


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
    def test_normal_reply_does_not_resume_or_reply_in_thread(self):
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

        with patch("handlers.message_handler.threading.Thread", ImmediateThread):
            handler.handle_event(event)

        self.assertIsNone(runner.calls[0]["resume_session_id"])
        self.assertFalse(messenger.replies[0]["reply_in_thread"])
        self.assertEqual(store.saved[0]["key"], "bot-m1")
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(messenger.reactions, [])

    def test_thread_followup_resumes_and_replies_in_thread(self):
        messenger = FakeMessenger()
        runner = FakeRunner()
        store = FakeSessionStore()
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
        self.assertEqual(runner.calls[0]["tool"], "qoder")
        self.assertTrue(messenger.replies[0]["reply_in_thread"])
        self.assertEqual({item["key"] for item in store.saved}, {"bot-m2", "thread-1"})


if __name__ == "__main__":
    unittest.main()
