from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from config import Config
import logger

STORE_VERSION = 2
LINK_RETENTION_PRIORITY = {
    "user_message": 0,
    "bot_reply": 1,
    "bot_file": 1,
    "thread": 2,
}


class SessionStoreError(RuntimeError):
    pass


class SessionStore:
    def __init__(self, config: Config) -> None:
        self.path = config.resolve_path("server.session_store.path")
        self.max_links = self._config_int(config, "server.session_store.max_links", 500, minimum=0)
        self.retention_days = self._config_int(config, "server.session_store.retention_days", 90, minimum=0)
        self.cleanup_interval_seconds = self._config_int(config, "server.session_store.cleanup_interval_seconds", 3600, minimum=1)
        self._lock = threading.RLock()
        self._next_cleanup_at = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cleanup(force=True)
        logger.debug(
            "会话存储已初始化：JSON 文件=%s 关联上限=%s",
            self.path,
            self.max_links,
        )

    def get(self, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        with self._lock:
            data, reset = self._load_locked()
            if reset:
                self._write_locked(data)
            return self._entry_for_key(data, key)

    def set(
        self,
        key: str,
        session_id: str,
        *,
        tool: str,
        model: str,
        link_type: str = "message",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not key or not session_id or not tool or not model:
            raise ValueError("会话关联必须包含键、会话 ID、CLI 和模型")
        now = int(time.time())
        with self._lock:
            data, _reset = self._load_locked()
            sessions = data["sessions"]
            links = data["links"]
            existing_session = sessions.get(session_id)
            if isinstance(existing_session, dict):
                existing_session["updated_at"] = now
            else:
                sessions[session_id] = {
                    "tool": tool,
                    "model": model,
                    "created_at": now,
                    "updated_at": now,
                }
            existing_link = links.get(key)
            created_at = self._timestamp(existing_link.get("created_at") if isinstance(existing_link, dict) else None, fallback=now)
            links[key] = {
                "session_id": session_id,
                "link_type": link_type,
                "metadata": metadata or {},
                "created_at": created_at,
                "updated_at": now,
            }
            self._enforce_limits(data)
            if time.monotonic() >= self._next_cleanup_at:
                self._cleanup_data(data, now)
                self._next_cleanup_at = time.monotonic() + self.cleanup_interval_seconds
            self._write_locked(data)
        logger.debug("已保存会话关联：键=%s 类型=%s 会话=%s", key, link_type, session_id)

    def resolve_for_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        for key in (message.get("thread_id"), message.get("root_id"), message.get("parent_id")):
            item = self.get(key)
            if item:
                return item
        return None

    def resolve_for_thread(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not message.get("thread_id"):
            return None
        for key in (message.get("thread_id"), message.get("root_id"), message.get("parent_id")):
            item = self.get(key)
            if item:
                return item
        return None

    def links_for_session(self, session_id: str) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            data, reset = self._load_locked()
            if reset:
                self._write_locked(data)
            entries = [
                entry
                for key in data["links"]
                if (entry := self._entry_for_key(data, key)) and entry["session_id"] == session_id
            ]
        return sorted(entries, key=lambda item: (item["link_updated_at"], item["key"]), reverse=True)

    def stats(self) -> dict[str, int | None]:
        with self._lock:
            data, reset = self._load_locked()
            if reset:
                self._write_locked(data)
            sessions = data["sessions"]
            updated_at = [
                self._timestamp(item.get("updated_at"))
                for item in sessions.values()
                if isinstance(item, dict)
            ]
        return {
            "sessions": len(sessions),
            "links": len(data["links"]),
            "oldest_session_updated_at": min(updated_at) if updated_at else None,
            "newest_session_updated_at": max(updated_at) if updated_at else None,
        }

    def cleanup(self, *, force: bool = False) -> dict[str, int]:
        with self._lock:
            now_monotonic = time.monotonic()
            if not force and now_monotonic < self._next_cleanup_at:
                return {"sessions": 0, "links": 0}
            data, reset = self._load_locked()
            removed = self._cleanup_data(data, int(time.time()))
            self._next_cleanup_at = now_monotonic + self.cleanup_interval_seconds
            if reset or removed["sessions"] or removed["links"] or not self.path.exists():
                self._write_locked(data)
        if removed["sessions"] or removed["links"]:
            logger.info("已清理过期会话数据：会话=%s 关联=%s", removed["sessions"], removed["links"])
        return removed

    def _load_locked(self) -> tuple[dict[str, Any], bool]:
        if not self.path.exists():
            return self._empty_data(), False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionStoreError(f"无法读取会话存储 {self.path}：{exc}") from exc
        return self._normalize_data(raw)

    def _normalize_data(self, raw: Any) -> tuple[dict[str, Any], bool]:
        if not isinstance(raw, dict):
            raise SessionStoreError("会话存储根节点必须是 JSON 对象")
        if raw.get("version") == STORE_VERSION:
            sessions = raw.get("sessions")
            links = raw.get("links")
            if not isinstance(sessions, dict) or not isinstance(links, dict):
                raise SessionStoreError("会话存储 version 2 缺少 sessions 或 links 对象")
            if not all(isinstance(session_id, str) and isinstance(session, dict) for session_id, session in sessions.items()):
                raise SessionStoreError("会话存储 sessions 格式无效")
            if not all(isinstance(key, str) and isinstance(link, dict) for key, link in links.items()):
                raise SessionStoreError("会话存储 links 格式无效")
            return {"version": STORE_VERSION, "sessions": sessions, "links": links}, False
        logger.warning("会话映射不是当前版本，将重置为空索引：%s", self.path)
        return self._empty_data(), True

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {"version": STORE_VERSION, "sessions": {}, "links": {}}

    def _cleanup_data(self, data: dict[str, Any], now: int) -> dict[str, int]:
        sessions_before = len(data["sessions"])
        links_before = len(data["links"])
        if self.retention_days:
            cutoff = now - self.retention_days * 24 * 60 * 60
            for session_id, session in list(data["sessions"].items()):
                if not isinstance(session, dict) or self._timestamp(session.get("updated_at")) < cutoff:
                    data["sessions"].pop(session_id, None)
        self._remove_orphan_links(data)
        self._enforce_limits(data)
        return {
            "sessions": sessions_before - len(data["sessions"]),
            "links": links_before - len(data["links"]),
        }

    def _enforce_limits(self, data: dict[str, Any]) -> None:
        self._limit_links(data)
        self._remove_orphan_sessions(data)

    def _limit_links(self, data: dict[str, Any]) -> None:
        self._remove_excess_links(data, list(data["links"]), self.max_links)

    def _remove_excess_links(self, data: dict[str, Any], keys: list[str], limit: int) -> None:
        excess = len(keys) - limit
        if excess <= 0:
            return
        removable = sorted(keys, key=lambda key: self._link_removal_key(key, data["links"][key]))
        for key in removable[:excess]:
            data["links"].pop(key, None)

    @staticmethod
    def _link_removal_key(key: str, link: Any) -> tuple[int, int, str]:
        if not isinstance(link, dict):
            return (-1, 0, key)
        priority = LINK_RETENTION_PRIORITY.get(str(link.get("link_type") or ""), 0)
        updated_at = SessionStore._timestamp(link.get("updated_at"))
        return priority, updated_at, key

    @staticmethod
    def _remove_orphan_links(data: dict[str, Any]) -> None:
        sessions = data["sessions"]
        for key, link in list(data["links"].items()):
            if not isinstance(link, dict) or link.get("session_id") not in sessions:
                data["links"].pop(key, None)

    @staticmethod
    def _remove_orphan_sessions(data: dict[str, Any]) -> None:
        linked_sessions = {
            link.get("session_id")
            for link in data["links"].values()
            if isinstance(link, dict) and isinstance(link.get("session_id"), str)
        }
        for session_id in list(data["sessions"]):
            if session_id not in linked_sessions:
                data["sessions"].pop(session_id, None)

    def _entry_for_key(self, data: dict[str, Any], key: str) -> dict[str, Any] | None:
        link = data["links"].get(key)
        if not isinstance(link, dict):
            return None
        session_id = link.get("session_id")
        session = data["sessions"].get(session_id)
        if not isinstance(session_id, str) or not isinstance(session, dict):
            return None
        tool = session.get("tool")
        model = session.get("model")
        if not isinstance(tool, str) or not isinstance(model, str):
            return None
        metadata = link.get("metadata")
        return {
            "key": key,
            "session_id": session_id,
            "tool": tool,
            "model": model,
            "link_type": str(link.get("link_type") or "message"),
            "metadata": metadata if isinstance(metadata, dict) else {},
            "created_at": self._timestamp(session.get("created_at")),
            "updated_at": self._timestamp(session.get("updated_at")),
            "link_created_at": self._timestamp(link.get("created_at")),
            "link_updated_at": self._timestamp(link.get("updated_at")),
        }

    def _write_locked(self, data: dict[str, Any]) -> None:
        temporary_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        temporary_path.replace(self.path)

    @staticmethod
    def _config_int(config: Config, dotted: str, default: int, *, minimum: int) -> int:
        value = int(config.get(dotted, default))
        if value < minimum:
            raise ValueError(f"{dotted} 不能小于 {minimum}")
        return value

    @staticmethod
    def _timestamp(value: Any, *, fallback: int | None = None) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback if fallback is not None else int(time.time())
