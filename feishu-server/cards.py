from __future__ import annotations

import json
import re
from typing import Any

MAX_SECTION_CHARS = 100_000
MARKDOWN_CHUNK_CHARS = 18_000
PLAIN_TEXT_CHUNK_CHARS = 9_000
MAX_CARD_BYTES = 30 * 1024
THINKING_RATIO = 0.75
CARD_WIDTHS = {"half", "full"}
_card_width = "half"
_tool_icons: dict[str, str] = {}


def configure_card_width(width: str) -> None:
    normalized = width.strip().lower()
    if normalized not in CARD_WIDTHS:
        raise ValueError(f"feishu.card_width 必须为以下值之一：{', '.join(sorted(CARD_WIDTHS))}")
    global _card_width
    _card_width = normalized


def configure_tool_icons(icons: dict[str, Any]) -> None:
    if not all(isinstance(tool, str) and isinstance(icon, str) and icon for tool, icon in icons.items()):
        raise ValueError("feishu.tool_icons 必须是 CLI 名称到非空图标字符串的映射")
    global _tool_icons
    _tool_icons = dict(icons)


def _limit_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 30].rstrip() + "\n\n... 内容过长，已截断 ..."


def _truncate(text: str, max_chars: int) -> str:
    return _limit_text(text, max_chars)


_TRUNCATE_PREFIX = "... 内容过长，已截断 ...\n\n"


def _truncate_front(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    prefix_bytes = _TRUNCATE_PREFIX.encode("utf-8")
    budget = max_bytes - len(prefix_bytes)
    if budget <= 0:
        return _TRUNCATE_PREFIX
    truncated = encoded[-budget:]
    # avoid splitting a multi-byte character
    while truncated and (truncated[0] & 0xC0) == 0x80:
        truncated = truncated[1:]
    return _TRUNCATE_PREFIX + truncated.decode("utf-8")


def _chunks(text: str, max_chars: int) -> list[str]:
    limited = _limit_text(text, MAX_SECTION_CHARS)
    if not limited:
        return ["（无内容）"]
    return [limited[index : index + max_chars] for index in range(0, len(limited), max_chars)]


def normalize_markdown(markdown: str) -> str:
    lines: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,2})\s+(.+)$", line)
        if match:
            lines.append(f"**{match.group(2).strip()}**")
        else:
            lines.append(line)
    return "\n".join(lines).strip() or "（无内容）"


def _base_card(title: str, template: str, elements: list[dict[str, Any]], summary: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {
        "update_multi": True,
        "summary": {"content": summary or title[:80]},
    }
    if _card_width == "full":
        config["width_mode"] = "fill"
    return {
        "schema": "2.0",
        "config": config,
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": title[:120],
            },
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def markdown_element(content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": _chunks(normalize_markdown(content), MARKDOWN_CHUNK_CHARS)[0],
        "text_size": "normal",
    }


def markdown_elements(content: str) -> list[dict[str, Any]]:
    return [
        {
            "tag": "markdown",
            "content": chunk,
            "text_size": "normal",
        }
        for chunk in _chunks(normalize_markdown(content), MARKDOWN_CHUNK_CHARS)
    ]


def plain_text_element(content: str, *, text_color: str = "default") -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": _chunks(content.strip() or "（无内容）", PLAIN_TEXT_CHUNK_CHARS)[0],
            "text_size": "normal",
            "text_color": text_color,
        },
    }


def plain_text_elements(content: str, *, text_color: str = "default") -> list[dict[str, Any]]:
    return [
        {
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": chunk,
                "text_size": "normal",
                "text_color": text_color,
            },
        }
        for chunk in _chunks(content.strip() or "（无内容）", PLAIN_TEXT_CHUNK_CHARS)
    ]


def _thinking_elements(thinking_text: str) -> list[dict[str, Any]]:
    thinking_title = f"分析过程 ({len(thinking_text)}字)"
    return [
        {"tag": "hr"},
        {
            "tag": "collapsible_panel",
            "expanded": False,
            "header": {
                "title": {"tag": "plain_text", "content": thinking_title},
                "vertical_align": "center",
                "icon": {
                    "tag": "standard_icon",
                    "token": "down-small-ccm_outlined",
                    "color": "",
                    "size": "16px 16px",
                },
                "icon_position": "right",
                "icon_expanded_angle": -180,
            },
            "border": {"color": "grey", "corner_radius": "5px"},
            "vertical_spacing": "8px",
            "padding": "8px 8px 8px 8px",
            "elements": plain_text_elements(thinking_text),
        },
    ]


def _card_bytes(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def build_ai_card(tool: str, model: str, result: str, thinking: str = "") -> dict[str, Any]:
    display_tool = tool.capitalize()
    title = f"{_tool_icons.get(tool, '🤖')}{display_tool} {model}"
    result = _limit_text(result, 10_000)
    elements = markdown_elements(result)
    thinking_text = thinking.strip()

    if thinking_text:
        probe = _base_card(title, "blue", list(elements), summary=_truncate(result.replace("\n", " "), 100))
        result_bytes = _card_bytes(probe)
        budget = min(int(MAX_CARD_BYTES * THINKING_RATIO), MAX_CARD_BYTES - result_bytes - 512)
        if budget > 0:
            truncated_thinking = _truncate_front(thinking_text, budget)
            elements.extend(_thinking_elements(truncated_thinking))

    card = _base_card(title, "blue", elements, summary=_truncate(result.replace("\n", " "), 100))

    if thinking_text and _card_bytes(card) > MAX_CARD_BYTES:
        elements = markdown_elements(result)
        probe = _base_card(title, "blue", list(elements), summary=_truncate(result.replace("\n", " "), 100))
        result_bytes = _card_bytes(probe)
        budget = MAX_CARD_BYTES - result_bytes - 1024
        if budget > 0:
            truncated_thinking = _truncate_front(thinking_text, budget)
            elements.extend(_thinking_elements(truncated_thinking))
        card = _base_card(title, "blue", elements, summary=_truncate(result.replace("\n", " "), 100))

    return card


def build_error_card(title: str, message: str, details: str = "") -> dict[str, Any]:
    content = message if not details else f"{message}\n\n{details}"
    return _base_card(f"⚠️ {title}", "red", plain_text_elements(content, text_color="red"), summary=message[:100])


def build_generic_card(title: str, content: str, template: str = "blue") -> dict[str, Any]:
    return _base_card(title, template, markdown_elements(content), summary=content.replace("\n", " ")[:100])


def build_timer_list_card(items: list[dict[str, Any]]) -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        status = "启用" if item.get("enabled", True) else "停用"
        name = item.get("name") or "未命名"
        lines = [
            f"**{name}**",
            f"ID：`{item.get('id', '')}`",
            f"Cron：`{item.get('cron', '')}`",
            f"类型：`{item.get('type', '')}`",
            f"状态：{status}",
            f"运行次数：{item.get('run_count', 0)}",
        ]
        if item.get("last_run_minute"):
            lines.append(f"上次运行：`{item['last_run_minute']}`")
        elements.extend(markdown_elements("\n".join(lines)))
        if index != len(items) - 1:
            elements.append({"tag": "hr"})
    return _base_card("定时任务列表", "blue", elements, summary=f"共 {len(items)} 个定时任务")


def build_running_tasks_card(items: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [
        "| ID | CLI / 模型 | PID | 已运行 |",
        "| --- | --- | ---: | ---: |",
    ]
    for item in items:
        lines.append(
            "| `{id}` | `{tool}` / `{model}` | {pid} | {elapsed}s |".format(
                id=item.get("message_id", ""),
                tool=item.get("tool", ""),
                model=item.get("model", ""),
                pid=item.get("pid", ""),
                elapsed=item.get("elapsed_seconds", 0),
            )
        )
    return _base_card("运行中的 AI 分析", "blue", markdown_elements("\n".join(lines)), summary=f"共 {len(items)} 个运行中任务")


def build_help_card(commands: list[tuple[str, str]]) -> dict[str, Any]:
    lines = ["**支持的操作**", ""]
    for command, desc in commands:
        lines.append(f"- `{command}`：{desc}")
    return build_generic_card("🤖 FeishuBot 帮助", "\n".join(lines), "blue")
