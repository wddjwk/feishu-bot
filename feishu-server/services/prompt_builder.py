from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Config
from services.messenger import FeishuApiError, Messenger


logger = logging.getLogger(__name__)


@dataclass
class PromptBuildResult:
    prompt: dict[str, Any]
    attached_files: list[dict[str, str]] = field(default_factory=list)


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("event", event)
    message = payload.get("message", payload)
    sender = payload.get("sender") or {}
    normalized = normalize_message(message)
    sender_id = sender.get("sender_id") or {}
    normalized["sender_type"] = sender.get("sender_type") or normalized.get("sender_type")
    normalized["sender_id"] = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
    normalized["tenant_key"] = sender.get("tenant_key") or normalized.get("tenant_key")
    return normalized


def normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    normalized = {
        "message_id": message.get("message_id"),
        "message_type": message.get("message_type") or message.get("msg_type"),
        "content": message.get("content") if "content" in message else body.get("content"),
        "create_time": message.get("create_time"),
        "update_time": message.get("update_time"),
        "parent_id": message.get("parent_id") or "",
        "root_id": message.get("root_id") or "",
        "thread_id": message.get("thread_id") or "",
        "chat_id": message.get("chat_id") or "",
        "chat_type": message.get("chat_type") or "",
        "mentions": message.get("mentions") if isinstance(message.get("mentions"), list) else body.get("mentions") or [],
        "sender_type": message.get("sender_type") or sender.get("sender_type"),
        "sender_id": sender.get("id") or message.get("sender_id"),
        "raw": message,
    }
    return normalized


def build_prompt(
    message: dict[str, Any],
    messenger: Messenger,
    config: Config,
    *,
    resume: bool = False,
) -> PromptBuildResult:
    user_input = extract_text(message).strip()

    if resume:
        attached_files = _download_attachments([message], messenger, config)
        return PromptBuildResult(
            {
                "user_input": user_input,
                "context": {
                    "message_id": message.get("message_id"),
                    "thread_id": message.get("thread_id"),
                    "conversation_history": [],
                    "root_reply_chain": [],
                    "attached_files": attached_files,
                    "resume": True,
                },
            },
            attached_files=attached_files,
        )

    context: dict[str, Any] = {
        "message_id": message.get("message_id"),
        "thread_id": message.get("thread_id"),
        "conversation_history": [],
        "root_reply_chain": [],
        "attached_files": [],
    }

    if message.get("thread_id"):
        thread_messages = messenger.list_messages(
            "thread",
            message["thread_id"],
            limit=int(config.get("prompt.thread_message_limit", 50)),
        )
        normalized = [normalize_message(item) for item in thread_messages]
        normalized.sort(key=lambda item: int(item.get("create_time") or 0))
        context["conversation_history"] = [_history_line(item) for item in normalized]
        context["attached_files"] = _download_attachments(normalized, messenger, config)
    elif message.get("parent_id"):
        chain = _trace_reply_chain(message, messenger, int(config.get("prompt.reply_chain_limit", 20)))
        chain.sort(key=lambda item: int(item.get("create_time") or 0))
        context["root_reply_chain"] = [_history_line(item) for item in chain]
        context["conversation_history"] = context["root_reply_chain"]
        context["attached_files"] = _download_attachments(chain, messenger, config)
    else:
        context["conversation_history"] = [_history_line(message)]
        context["attached_files"] = _download_attachments([message], messenger, config)

    return PromptBuildResult({"user_input": user_input, "context": context}, attached_files=context["attached_files"])


def extract_text(message: dict[str, Any]) -> str:
    msg_type = message.get("message_type") or message.get("msg_type") or ""
    content = _loads_content(message.get("content"))
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    if msg_type == "text":
        return str(content.get("text") or "")
    if msg_type == "post":
        return _extract_post_text(content)
    if msg_type == "interactive":
        return _extract_card_text(content)
    if "text" in content:
        return str(content.get("text") or "")
    if "summary" in content and isinstance(content["summary"], dict):
        return _extract_post_text(content["summary"])
    return ""


def extract_files(message: dict[str, Any]) -> list[dict[str, str]]:
    msg_type = message.get("message_type") or message.get("msg_type") or ""
    content = _loads_content(message.get("content"))
    files: list[dict[str, str]] = []
    if not isinstance(content, dict):
        return files
    if msg_type == "image" and content.get("image_key"):
        files.append({"key": content["image_key"], "type": "image", "name": "image"})
    if msg_type in {"file", "audio", "media"} and content.get("file_key"):
        files.append({"key": content["file_key"], "type": "file", "name": content.get("file_name") or msg_type})
    if msg_type == "post":
        for item in _walk(content):
            if not isinstance(item, dict):
                continue
            if item.get("tag") == "img" and item.get("image_key"):
                files.append({"key": item["image_key"], "type": "image", "name": "image"})
            if item.get("file_key"):
                files.append({"key": item["file_key"], "type": "file", "name": item.get("file_name") or "file"})
    return files


def _trace_reply_chain(message: dict[str, Any], messenger: Messenger, limit: int) -> list[dict[str, Any]]:
    chain = [message]
    seen = {message.get("message_id")}
    parent_id = message.get("parent_id")
    while parent_id and len(chain) < limit and parent_id not in seen:
        parent = messenger.get_message(parent_id)
        if not parent:
            break
        normalized = normalize_message(parent)
        chain.append(normalized)
        seen.add(parent_id)
        parent_id = normalized.get("parent_id")
    return chain


def _download_attachments(messages: list[dict[str, Any]], messenger: Messenger, config: Config) -> list[dict[str, str]]:
    attached: list[dict[str, str]] = []
    download_dir = config.resolve_path("prompt.download_dir")
    for message in messages:
        message_id = message.get("message_id")
        if not message_id:
            continue
        for file_item in extract_files(message):
            safe_name = _safe_filename(file_item.get("name") or file_item["key"])
            dest = download_dir / str(message_id) / safe_name
            try:
                path = messenger.download_message_resource(message_id, file_item["key"], file_item["type"], dest)
            except FeishuApiError as exc:
                logger.warning("下载消息资源失败：消息=%s 文件=%s 错误=%s", message_id, file_item["key"], exc)
                attached.append({"name": safe_name, "path": "", "note": f"download failed: {exc}"})
                continue
            attached.append({"name": safe_name, "path": str(path), "note": f"from message {message_id}"})
    return attached


def _history_line(message: dict[str, Any]) -> str:
    ts = _format_time(message.get("create_time"))
    role = "assistant" if (message.get("sender_type") in {"bot", "app"}) else "user"
    text = extract_text(message).strip()
    file_names = [item["name"] for item in extract_files(message)]
    if file_names:
        text = f"{text}\n[files: {', '.join(file_names)}]".strip()
    return f"[{ts}][{role}]: {text or '（无文本内容）'}"


def _format_time(value: Any) -> str:
    try:
        seconds = int(value) / 1000
        return datetime.fromtimestamp(seconds).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "unknown-time"


def _loads_content(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def _extract_post_text(content: dict[str, Any]) -> str:
    lines: list[str] = []
    title = content.get("title")
    if title:
        lines.append(str(title))
    for row in content.get("content", []):
        parts: list[str] = []
        for item in row if isinstance(row, list) else [row]:
            if not isinstance(item, dict):
                continue
            tag = item.get("tag")
            if tag in {"text", "a"}:
                parts.append(str(item.get("text", "")))
            elif tag == "at":
                parts.append(str(item.get("user_name") or item.get("user_id") or "@user"))
            elif tag == "code_block":
                parts.append(str(item.get("text", "")))
            elif tag == "img":
                parts.append("[image]")
            elif tag == "media":
                parts.append("[media]")
            elif tag == "emotion":
                parts.append(f":{item.get('emoji_type', 'emoji')}:")
        if parts:
            lines.append("".join(parts))
    return "\n".join(lines)


def _extract_card_text(content: dict[str, Any]) -> str:
    values: list[str] = []
    for item in _walk(content):
        if not isinstance(item, dict):
            continue
        for key in ("content", "text", "title", "placeholder"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
            elif isinstance(value, dict):
                nested = value.get("content") or value.get("text")
                if isinstance(nested, str) and nested.strip():
                    values.append(nested.strip())
    return "\n".join(dict.fromkeys(values))


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:80] or "file"
