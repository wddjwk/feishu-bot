from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from services.ai_runner import TokenUsage

MAX_SECTION_CHARS = 100_000
MARKDOWN_CHUNK_CHARS = 18_000
PLAIN_TEXT_CHUNK_CHARS = 9_000
MAX_CARD_BYTES = 30 * 1024
THINKING_RATIO = 0.75
CODE_COLLAPSE_LINES = 20
CARD_WIDTHS = {"half", "full"}
_card_width = "half"
_tool_icons: dict[str, str] = {}


def configure_card_width(width: str) -> None:
    normalized = width.strip().lower()
    if normalized not in CARD_WIDTHS:
        raise ValueError(f"options.card_width 必须为以下值之一：{', '.join(sorted(CARD_WIDTHS))}")
    global _card_width
    _card_width = normalized


def configure_tool_icons(icons: dict[str, Any]) -> None:
    if not all(isinstance(tool, str) and isinstance(icon, str) and icon for tool, icon in icons.items()):
        raise ValueError("工具图标必须是 CLI 名称到非空图标字符串的映射")
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


def _split_code_segments(markdown: str) -> list[tuple[str, ...]]:
    """Split markdown into ordered ("text", content) / ("code", fenced, lang, lines) segments."""
    segments: list[tuple[str, ...]] = []
    text_buf: list[str] = []
    code_buf: list[str] = []
    fence_len = 0
    lang = ""
    in_code = False

    def flush_text() -> None:
        content = "\n".join(text_buf).strip()
        if content:
            segments.append(("text", content))
        text_buf.clear()

    for line in markdown.split("\n"):
        stripped = line.strip()
        if in_code:
            code_buf.append(line)
            if re.match(r"^`{3,}$", stripped) and len(stripped) >= fence_len:
                in_code = False
                segments.append(("code", "\n".join(code_buf), lang, len(code_buf) - 2))
                code_buf = []
        elif stripped.startswith("```"):
            flush_text()
            in_code = True
            fence_len = len(stripped) - len(stripped.lstrip("`"))
            lang = stripped.lstrip("`").strip()
            code_buf = [line]
        else:
            text_buf.append(line)

    if in_code and code_buf:
        segments.append(("text", "\n".join(code_buf).strip()))
    flush_text()
    return segments


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


def _code_collapsible(fenced: str, lang: str, num_lines: int) -> dict[str, Any]:
    title = f"{lang} 代码（{num_lines}行）" if lang else f"代码（{num_lines}行）"
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "plain_text", "content": title},
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
        "elements": [
            {"tag": "markdown", "content": fenced, "text_size": "normal"},
        ],
    }


def markdown_elements(content: str) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for segment in _split_code_segments(content):
        if segment[0] == "text":
            for chunk in _chunks(normalize_markdown(segment[1]), MARKDOWN_CHUNK_CHARS):
                elements.append({"tag": "markdown", "content": chunk, "text_size": "normal"})
        else:
            _, fenced, lang, num_lines = segment
            if num_lines > CODE_COLLAPSE_LINES:
                elements.append(_code_collapsible(fenced, lang, num_lines))
            else:
                elements.append({"tag": "markdown", "content": fenced, "text_size": "normal"})
    return elements or [{"tag": "markdown", "content": "（无内容）", "text_size": "normal"}]


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


def _thinking_elements(thinking_text: str, usage: "TokenUsage | None" = None) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = [{"tag": "hr"}]
    usage_el = _usage_element(usage)
    if usage_el:
        elements.append(usage_el)
    thinking_title = f"分析过程 ({len(thinking_text)}字)"
    elements.append({
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
    })
    return elements


def _card_bytes(card: dict[str, Any]) -> int:
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))


def _format_count(n: int | None) -> str:
    if n is None or n <= 0:
        return "0"
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        value = n / 1000
        rounded = round(value)
        return f"{value:.1f}K" if rounded < 10 else f"{rounded}K"
    value = n / 1_000_000
    rounded = round(value)
    return f"{value:.1f}M" if rounded < 10 else f"{rounded}M"


def _usage_element(usage: "TokenUsage | None") -> dict[str, Any] | None:
    """Build a compact token-usage element for the card footer (statusline style)."""
    if usage is None:
        return None
    input_t = getattr(usage, "input_tokens", None)
    cached_t = getattr(usage, "cached_tokens", None)
    output_t = getattr(usage, "output_tokens", None)
    parts: list[str] = []
    if isinstance(input_t, int) and input_t > 0:
        parts.append("↑ " + _format_count(input_t))
    if isinstance(cached_t, int) and cached_t > 0:
        parts.append("⬢ " + _format_count(cached_t))
    if isinstance(output_t, int) and output_t > 0:
        parts.append("↓ " + _format_count(output_t))
    if not parts:
        return None
    return {
        "tag": "markdown",
        "content": "  ·  ".join(parts),
        "text_size": "small",
    }


def build_ai_card(tool: str, model: str, result: str, thinking: str = "", usage: "TokenUsage | None" = None) -> dict[str, Any]:
    display_tool = tool.capitalize()
    title = f"{_tool_icons.get(tool, '🤖')}{display_tool} {model}"
    result = _limit_text(result, 10_000)
    elements = markdown_elements(result)
    thinking_text = thinking.strip()
    usage_el = _usage_element(usage)

    if thinking_text:
        probe = _base_card(title, "blue", list(elements), summary=_truncate(result.replace("\n", " "), 100))
        result_bytes = _card_bytes(probe)
        budget = min(int(MAX_CARD_BYTES * THINKING_RATIO), MAX_CARD_BYTES - result_bytes - 512)
        if budget > 0:
            truncated_thinking = _truncate_front(thinking_text, budget)
            elements.extend(_thinking_elements(truncated_thinking, usage))
    elif usage_el:
        elements.append({"tag": "hr"})
        elements.append(usage_el)

    card = _base_card(title, "blue", elements, summary=_truncate(result.replace("\n", " "), 100))

    if thinking_text and _card_bytes(card) > MAX_CARD_BYTES:
        elements = markdown_elements(result)
        probe = _base_card(title, "blue", list(elements), summary=_truncate(result.replace("\n", " "), 100))
        result_bytes = _card_bytes(probe)
        budget = MAX_CARD_BYTES - result_bytes - 1024
        if budget > 0:
            truncated_thinking = _truncate_front(thinking_text, budget)
            elements.extend(_thinking_elements(truncated_thinking, usage))
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
