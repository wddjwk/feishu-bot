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

FILE_DELIVERY_PROTOCOL = (
    "如需将生成或找到的本地文件交付给用户，请在最终回复中每行输出一个 "
    "[[FEISHU_FILE:/agent-workspace 内的绝对路径]] 标记。服务会先发送该文件，再回复文件说明完成情况。"
)
IMAGE_KEY_PATTERN = re.compile(r"\bimg_[A-Za-z0-9_-]+\b")
FILE_KEY_PATTERN = re.compile(r"\bfile_[A-Za-z0-9_-]+\b")


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
    return {
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


def is_standalone_file_message(message: dict[str, Any]) -> bool:
    return message.get("message_type") == "file" and not message.get("parent_id")


def build_prompt(
    message: dict[str, Any],
    messenger: Messenger,
    config: Config,
    *,
    resume: bool = False,
) -> PromptBuildResult:
    if resume:
        messages = _resume_context_messages(message, messenger, config)
        attachments, references = _download_attachments(messages, messenger, config)
        user_input = extract_text(message, resource_references=references).strip()
        return PromptBuildResult(
            {
                "user_input": user_input,
                "context": {
                    "message_id": message.get("message_id"),
                    "thread_id": message.get("thread_id"),
                    "conversation_history": [],
                    "root_reply_chain": [],
                    "attached_files": attachments,
                    "source_messages": _source_messages(messages),
                    "file_delivery_protocol": FILE_DELIVERY_PROTOCOL,
                    "resume": True,
                },
            },
            attached_files=attachments,
        )

    if message.get("thread_id"):
        history_limit = int(config.get("prompt.thread_message_limit", 50))
        messages = messenger.list_messages(
            "thread",
            message["thread_id"],
            limit=history_limit,
        )
        messages = [normalize_message(item) for item in messages]
        if not any(item.get("message_id") == message.get("message_id") for item in messages):
            messages.append(message)
        messages.sort(key=lambda item: int(item.get("create_time") or 0))
        if len(messages) > history_limit:
            messages = messages[-history_limit:]
        history_kind = "thread"
    elif message.get("parent_id"):
        messages = _trace_reply_chain(message, messenger, int(config.get("prompt.reply_chain_limit", 20)))
        messages.sort(key=lambda item: int(item.get("create_time") or 0))
        history_kind = "reply_chain"
    else:
        messages = [message]
        history_kind = "single"

    attachments, references = _download_attachments(messages, messenger, config)
    history = [_history_line(item, references) for item in messages]
    user_input = extract_text(message, resource_references=references).strip()
    context: dict[str, Any] = {
        "message_id": message.get("message_id"),
        "thread_id": message.get("thread_id"),
        "conversation_history": history,
        "root_reply_chain": history if history_kind == "reply_chain" else [],
        "attached_files": attachments,
        "source_messages": _source_messages(messages),
        "file_delivery_protocol": FILE_DELIVERY_PROTOCOL,
    }
    return PromptBuildResult({"user_input": user_input, "context": context}, attached_files=attachments)


def extract_text(
    message: dict[str, Any],
    *,
    resource_references: dict[str, dict[str, str]] | None = None,
) -> str:
    content = _loads_content(message.get("content"))
    message_id = str(message.get("message_id") or "")
    references = (resource_references or {}).get(message_id, {})
    return _render_content(
        str(message.get("message_type") or message.get("msg_type") or ""),
        content,
        references,
    )


def extract_files(message: dict[str, Any]) -> list[dict[str, str]]:
    msg_type = str(message.get("message_type") or message.get("msg_type") or "")
    if msg_type == "sticker":
        return []
    content = _loads_content(message.get("content"))
    if not isinstance(content, dict):
        return []

    files: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in _walk(content):
        if not isinstance(node, dict):
            continue
        tag = str(node.get("tag") or "")
        if tag == "sticker":
            continue
        if tag == "md":
            markdown = node.get("text")
            if isinstance(markdown, str):
                for key in IMAGE_KEY_PATTERN.findall(markdown):
                    _add_resource(files, seen, key, "image", "image")
                for key in FILE_KEY_PATTERN.findall(markdown):
                    _add_resource(files, seen, key, "file", "file")
        image_key = node.get("image_key")
        if isinstance(image_key, str) and image_key:
            _add_resource(files, seen, image_key, "image", _resource_name(node, "image"))
        file_key = node.get("file_key")
        if isinstance(file_key, str) and file_key:
            _add_resource(files, seen, file_key, "file", _resource_name(node, _resource_kind_name(msg_type, tag)))
    return files


def _add_resource(
    resources: list[dict[str, str]],
    seen: set[tuple[str, str]],
    key: str,
    resource_type: str,
    name: str,
) -> None:
    identity = (key, resource_type)
    if identity in seen:
        return
    seen.add(identity)
    resources.append({"key": key, "type": resource_type, "name": name})


def _resume_context_messages(message: dict[str, Any], messenger: Messenger, config: Config) -> list[dict[str, Any]]:
    if not message.get("parent_id"):
        return [message]
    return _trace_reply_chain(message, messenger, int(config.get("prompt.reply_chain_limit", 20)))


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


def _download_attachments(
    messages: list[dict[str, Any]],
    messenger: Messenger,
    config: Config,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    attached: list[dict[str, str]] = []
    references: dict[str, dict[str, str]] = {}
    download_dir = config.resolve_path("prompt.download_dir")
    for message in messages:
        message_id = str(message.get("message_id") or "")
        if not message_id:
            continue
        per_message = references.setdefault(message_id, {})
        for index, file_item in enumerate(extract_files(message), start=1):
            key = file_item["key"]
            if key in per_message:
                continue
            safe_name = _safe_filename(file_item.get("name") or key)
            destination = download_dir / message_id / f"{index:02d}_{safe_name}"
            try:
                path = messenger.download_message_resource(message_id, key, file_item["type"], destination)
            except FeishuApiError as exc:
                logger.warning("下载消息资源失败：消息=%s 文件=%s 错误=%s", message_id, key, exc)
                attached.append(
                    {
                        "message_id": message_id,
                        "key": key,
                        "name": safe_name,
                        "type": file_item["type"],
                        "path": "",
                        "note": f"download failed: {exc}",
                    }
                )
                per_message[key] = f"[资源下载失败：{key}]"
                continue
            reference = f"@{path}"
            per_message[key] = reference
            attached.append(
                {
                    "message_id": message_id,
                    "key": key,
                    "name": path.name,
                    "type": file_item["type"],
                    "path": str(path),
                    "note": f"from message {message_id}",
                }
            )
    return attached, references


def _render_content(message_type: str, content: Any, references: dict[str, str]) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    if message_type == "text":
        return str(content.get("text") or "")
    if message_type == "post":
        return _render_post(content, references)
    if message_type in {"image", "file", "audio", "media"}:
        return _render_media_content(content, references)
    if message_type == "interactive":
        return _render_structured_content(content, references)
    if "text" in content:
        return str(content.get("text") or "")
    if "summary" in content and isinstance(content["summary"], dict):
        return _render_post(content["summary"], references)
    return _render_structured_content(content, references)


def _render_post(content: dict[str, Any], references: dict[str, str]) -> str:
    post = _post_locale_content(content)
    lines: list[str] = []
    title = post.get("title")
    if isinstance(title, str) and title:
        lines.append(title)
    rows = post.get("content_v2") or post.get("content") or []
    if not isinstance(rows, list):
        return "\n".join(lines)
    for row in rows:
        nodes = row if isinstance(row, list) else [row]
        rendered = "".join(_render_node(node, references) for node in nodes)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


def _post_locale_content(content: dict[str, Any]) -> dict[str, Any]:
    if isinstance(content.get("zh_cn"), dict):
        return content["zh_cn"]
    for value in content.values():
        if isinstance(value, dict) and ("content" in value or "content_v2" in value):
            return value
    return content


def _render_media_content(content: dict[str, Any], references: dict[str, str]) -> str:
    parts: list[str] = []
    for key in ("file_key", "image_key"):
        resource_key = content.get(key)
        if isinstance(resource_key, str) and resource_key:
            parts.append(references.get(resource_key, f"[资源：{resource_key}]"))
    return "\n".join(dict.fromkeys(parts))


def _render_node(node: Any, references: dict[str, str]) -> str:
    if not isinstance(node, dict):
        return str(node) if isinstance(node, str) else ""
    tag = str(node.get("tag") or "")
    if tag in {"text", "md"}:
        return _replace_resource_keys(str(node.get("text") or ""), references)
    if tag == "a":
        text = str(node.get("text") or "")
        href = str(node.get("href") or "")
        return f"[{text}]({href})" if href else text
    if tag == "at":
        user_id = str(node.get("user_id") or "")
        name = str(node.get("user_name") or user_id or "@user")
        return f"<at user_id=\"{user_id}\">{name}</at>"
    if tag == "img":
        image_key = str(node.get("image_key") or "")
        return references.get(image_key, f"[图片：{image_key}]")
    if tag in {"media", "file", "audio"}:
        return _render_media_content(node, references)
    if tag == "emotion":
        return f":{node.get('emoji_type', 'emoji')}:"
    if tag == "code":
        return f"`{node.get('text', '')}`"
    if tag == "code_block":
        language = str(node.get("language") or "").lower()
        return f"\n```{language}\n{node.get('text', '')}\n```\n"
    if tag == "hr":
        return "\n---\n"
    if tag:
        return f"[飞书标签 {tag}：{json.dumps(node, ensure_ascii=False, sort_keys=True)}]"
    return json.dumps(node, ensure_ascii=False, sort_keys=True)


def _render_structured_content(content: dict[str, Any], references: dict[str, str]) -> str:
    lines: list[str] = []
    for node in _walk(content):
        if not isinstance(node, dict) or "tag" not in node:
            continue
        rendered = _render_node(node, references)
        if rendered:
            lines.append(rendered)
    if lines:
        return "\n".join(dict.fromkeys(lines))
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _replace_resource_keys(text: str, references: dict[str, str]) -> str:
    for key, reference in references.items():
        text = text.replace(key, reference)
    return text


def _history_line(message: dict[str, Any], references: dict[str, dict[str, str]]) -> str:
    ts = _format_time(message.get("create_time"))
    role = "assistant" if message.get("sender_type") in {"bot", "app"} else "user"
    text = extract_text(message, resource_references=references).strip()
    return f"[{ts}][{role}]: {text or '（无文本内容）'}"


def _source_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": message.get("message_id"),
            "message_type": message.get("message_type"),
            "content": _loads_content(message.get("content")),
            "mentions": message.get("mentions") if isinstance(message.get("mentions"), list) else [],
        }
        for message in messages
    ]


def _format_time(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "unknown-time"


def _loads_content(content: Any) -> Any:
    if isinstance(content, str):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return content
    return content


def _walk(value: Any) -> list[Any]:
    items = [value]
    if isinstance(value, dict):
        for child in value.values():
            items.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            items.extend(_walk(child))
    return items


def _resource_name(node: dict[str, Any], fallback: str) -> str:
    for key in ("file_name", "name", "title"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return fallback


def _resource_kind_name(message_type: str, tag: str) -> str:
    if tag == "media" or message_type == "media":
        return "video"
    if tag == "audio" or message_type == "audio":
        return "audio"
    return "file"


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return cleaned[:80] or "resource"
