from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from config import Config


class SessionStore:
    def __init__(self, config: Config) -> None:
        self.path = config.resolve_path("session_store.path")
        self.max_entries = int(config.get("session_store.max_entries", 500))
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"entries": []})

    def get(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        with self._lock:
            data = self._read()
            for item in data.get("entries", []):
                if item.get("key") == key:
                    return dict(item)
        return None

    def set(
        self,
        key: str,
        session_id: str,
        *,
        tool: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        with self._lock:
            data = self._read()
            entries = [item for item in data.get("entries", []) if item.get("key") != key]
            entries.append(
                {
                    "key": key,
                    "session_id": session_id,
                    "tool": tool,
                    "model": model,
                    "metadata": metadata or {},
                    "created_at": now,
                    "updated_at": now,
                }
            )
            entries.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
            data["entries"] = entries[: self.max_entries]
            self._write(data)

    def resolve_for_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        for key in (message.get("thread_id"), message.get("root_id"), message.get("parent_id")):
            item = self.get(key)
            if item:
                return item
        return None

    def resolve_for_thread(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not message.get("thread_id"):
            return None
        for key in (message.get("thread_id"), message.get("root_id")):
            item = self.get(key)
            if item:
                return item
        return None

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"entries": []}
        with self.path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
