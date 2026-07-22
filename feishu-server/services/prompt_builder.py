from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from config import Config
from services.messenger import FeishuApiError, Messenger
import logger

IMAGE_KEY_PATTERN = re.compile(r"\bimg_[A-Za-z0-9_-]+\b")
FILE_KEY_PATTERN = re.compile(r"\bfile_[A-Za-z0-9_-]+\b")


@dataclass
class PromptBuildResult:
    prompt: str
    work_dir: Path
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
    sender_id = sender.get("id") or sender.get("sender_id") or message.get("sender_id")
    if isinstance(sender_id, dict):
        sender_id = sender_id.get("open_id") or sender_id.get("user_id") or sender_id.get("union_id")
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
        "sender_id": sender_id or "",
        "raw": message,
    }


def is_standalone_file_message(message: dict[str, Any]) -> bool:
    return message.get("message_type") == "file" and not message.get("parent_id")


def build_prompt(
    message: dict[str, Any],
    messenger: Messenger,
    config: Config,
) -> PromptBuildResult:
    message_id = str(message.get("message_id") or "")
    reply_chain = _trace_reply_chain(message, messenger) if message.get("parent_id") else [message]
    root_message_id = _root_message_id(message, reply_chain)
    work_dir = _work_dir(config, root_message_id)
    group_thread = _group_thread_context(message, messenger, config) if message.get("thread_id") and message.get("chat_type") == "group" else []
    attachment_messages = _merge_messages(reply_chain, group_thread)
    attachments, references = _download_attachments(attachment_messages, messenger, work_dir)
    logger.debug(
        "Prompt 构建中：消息=%s 回复链=%s 群上下文=%s 附件=%s",
        message_id,
        len(reply_chain),
        len(group_thread),
        len(attachments),
    )

    user_input = extract_text(message, resource_references=references).strip()
    user_input = _append_attachment_references(
        user_input,
        attachments,
        message_ids={str(item.get("message_id") or "") for item in reply_chain},
    )

    if group_thread:
        context_messages = _without_message(group_thread, message.get("message_id"))
        for item in _without_message(reply_chain, message.get("message_id")):
            if not any(item.get("message_id") == existing.get("message_id") for existing in context_messages):
                context_messages.append(item)
        context_messages.sort(key=lambda item: int(item.get("create_time") or 0))
    elif not message.get("thread_id"):
        context_messages = _without_message(reply_chain, message.get("message_id"))
        context_messages.sort(key=lambda item: int(item.get("create_time") or 0))
    else:
        context_messages = []

    prompt: dict[str, Any] = {
        "workfolder": _relative_workfolder(work_dir),
        "user_id": str(message.get("sender_id") or "unknown"),
        "user_input": user_input or "（无文本内容）",
        "context": [_history_line(item, references) for item in context_messages],
    }
    if message.get("chat_type") == "group":
        prompt["group_id"] = str(message.get("chat_id") or "")
    prompt_json = json.dumps(prompt, ensure_ascii=False, indent=2)
    logger.debug(
        "Prompt 构建完成：消息=%s 上下文条数=%s prompt长度=%s 工作目录=%s",
        message_id,
        len(prompt["context"]),
        len(prompt_json),
        work_dir.name,
    )
    return PromptBuildResult(
        prompt_json,
        work_dir=work_dir,
        attached_files=attachments,
    )


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


def _trace_reply_chain(message: dict[str, Any], messenger: Messenger) -> list[dict[str, Any]]:
    chain = [message]
    seen = {message.get("message_id")}
    parent_id = message.get("parent_id")
    while parent_id and parent_id not in seen:
        parent = messenger.get_message(parent_id)
        if not parent:
            break
        normalized = normalize_message(parent)
        chain.append(normalized)
        seen.add(parent_id)
        parent_id = normalized.get("parent_id")
    return chain


def _group_thread_context(message: dict[str, Any], messenger: Messenger, config: Config) -> list[dict[str, Any]]:
    limit = int(config.get("prompt.group_thread_context_limit", 100))
    if limit <= 0:
        return [message]
    messages = [
        normalize_message(item)
        for item in messenger.list_messages(
            "thread",
            message["thread_id"],
            limit=limit,
            sort_type="ByCreateTimeDesc",
        )
    ]
    if not any(item.get("message_id") == message.get("message_id") for item in messages):
        messages.append(message)
    messages.sort(key=lambda item: int(item.get("create_time") or 0))
    return messages[-limit:]


def _merge_messages(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group:
            key = str(item.get("message_id") or "")
            if key:
                merged[key] = item
    return sorted(merged.values(), key=lambda item: int(item.get("create_time") or 0))


def _root_message_id(message: dict[str, Any], reply_chain: list[dict[str, Any]]) -> str:
    root_id = message.get("root_id")
    if isinstance(root_id, str) and root_id:
        return root_id
    for item in reversed(reply_chain):
        message_id = item.get("message_id")
        if isinstance(message_id, str) and message_id:
            return message_id
    return str(message.get("message_id") or "unknown")


def _work_dir(config: Config, root_message_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_-]+", "_", root_message_id).strip("._")
    work_root = config.resolve_path("prompt.work_root")
    work_dir = work_root / (safe_id or "unknown")
    return work_dir.resolve()


def _relative_workfolder(work_dir: Path) -> str:
    return (Path("workfolder") / work_dir.name).as_posix()


def _without_message(messages: list[dict[str, Any]], message_id: Any) -> list[dict[str, Any]]:
    return [item for item in messages if item.get("message_id") != message_id]


def _append_attachment_references(
    user_input: str,
    attachments: list[dict[str, str]],
    *,
    message_ids: set[str],
) -> str:
    references = [
        f"@{item['path']}"
        for item in attachments
        if item.get("path") and item.get("message_id") in message_ids and f"@{item['path']}" not in user_input
    ]
    if not references:
        return user_input
    prefix = "\n\n" if user_input else ""
    return f"{user_input}{prefix}" + "\n".join(references)


def _cached_resource_path(destination: Path) -> Path | None:
    if destination.is_file():
        return destination
    candidates = sorted(destination.parent.glob("resource_*"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates and candidates[0].is_file() else None


def _download_attachments(
    messages: list[dict[str, Any]],
    messenger: Messenger,
    work_dir: Path,
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    attached: list[dict[str, str]] = []
    references: dict[str, dict[str, str]] = {}
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
            cache_key = sha256(f"{message_id}:{key}:{file_item['type']}".encode("utf-8")).hexdigest()[:24]
            destination = work_dir / "attachments" / cache_key / f"resource_{index:02d}_{safe_name}"
            try:
                path = _cached_resource_path(destination) or messenger.download_message_resource(
                    message_id,
                    key,
                    file_item["type"],
                    destination,
                )
                if path.exists():
                    path.touch()
                    path.parent.touch()
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
                per_message[key] = "[附件下载失败]"
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
            parts.append(references.get(resource_key, "[附件不可用]"))
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
        name = str(node.get("user_name") or "@成员")
        return f"@{name.lstrip('@')}"
    if tag == "img":
        image_key = str(node.get("image_key") or "")
        return references.get(image_key, "[附件不可用]")
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
        return f"[飞书组件：{tag}]"
    return ""


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
    return "（富消息）"


def _replace_resource_keys(text: str, references: dict[str, str]) -> str:
    for key, reference in references.items():
        text = text.replace(key, reference)
    return text


def _history_line(message: dict[str, Any], references: dict[str, dict[str, str]]) -> str:
    ts = _format_time(message.get("create_time"))
    text = extract_text(message, resource_references=references).strip()
    sender_id = message.get("sender_id") or "unknown"
    return f"[{ts}][{sender_id}]: {text or '（无文本内容）'}"


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
