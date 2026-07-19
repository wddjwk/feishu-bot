import json
import tempfile
import unittest
from pathlib import Path

from services.session_store import STORE_VERSION, SessionStore


class FakeConfig:
    def __init__(self, root: Path, **options) -> None:
        self.root = root
        self.options = {
            "session_store.path": "data/session_map.json",
            "session_store.max_links": 10,
            "session_store.retention_days": 90,
            "session_store.cleanup_interval_seconds": 1,
            **options,
        }

    def resolve_path(self, dotted: str) -> Path:
        if dotted != "session_store.path":
            raise AssertionError(f"unexpected path config: {dotted}")
        return (self.root / self.options[dotted]).resolve()

    def get(self, dotted: str, default=None):
        return self.options.get(dotted, default)


class SessionStoreTests(unittest.TestCase):
    def test_first_topic_message_resolves_the_parent_bot_card_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(FakeConfig(Path(tmp)))
            store.set("bot-card", "session-1", tool="claude", model="model-a", link_type="bot_reply")

            resolved = store.resolve_for_thread(
                {
                    "thread_id": "new-thread",
                    "root_id": "",
                    "parent_id": "bot-card",
                }
            )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["session_id"], "session-1")
        self.assertEqual(resolved["tool"], "claude")
        self.assertEqual(resolved["model"], "model-a")
        self.assertEqual(resolved["link_type"], "bot_reply")

    def test_resets_legacy_flat_json_without_creating_a_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_path = root / "data" / "session_map.json"
            store_path.parent.mkdir(parents=True)
            store_path.write_text(
                json.dumps(
                    {
                        "entries": [
                            {
                                "key": "bot-card",
                                "session_id": "session-1",
                                "tool": "claude",
                                "model": "model-a",
                                "metadata": {"source_message_id": "user-message"},
                                "created_at": 1,
                                "updated_at": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            store = SessionStore(FakeConfig(root))
            persisted = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertFalse((root / "data" / "session_map.json.v1.bak").exists())

        self.assertEqual(persisted["version"], STORE_VERSION)
        self.assertEqual(persisted["sessions"], {})
        self.assertEqual(persisted["links"], {})

    def test_bounds_links_and_cleans_expired_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                FakeConfig(
                    root,
                    **{
                        "session_store.max_links": 2,
                        "session_store.retention_days": 1,
                    },
                )
            )
            for key in ("one", "two", "three"):
                store.set(key, "session-1", tool="claude", model="model-a", link_type="user_message")
            self.assertEqual(len(store.links_for_session("session-1")), 2)

            raw = json.loads(store.path.read_text(encoding="utf-8"))
            raw["sessions"]["session-1"]["updated_at"] = 0
            store.path.write_text(json.dumps(raw), encoding="utf-8")
            result = store.cleanup(force=True)

            self.assertEqual(result["sessions"], 1)
            self.assertEqual(store.stats()["links"], 0)

if __name__ == "__main__":
    unittest.main()
