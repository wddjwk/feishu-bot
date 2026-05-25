from __future__ import annotations

import re
from typing import Any


TOOL_ICONS = {
    "qoder": "💻",
    "claude": "🧠",
    "copilot": "🛩️",
    "codebuddy": "🤝",
}


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 30].rstrip() + "\n\n... 内容过长，已截断 ..."


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
    return {
        "schema": "2.0",
        "config": {
            "update_multi": True,
            "width_mode": "fill",
            "summary": {"content": summary or title[:80]},
        },
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
        "content": _truncate(normalize_markdown(content), 18000),
        "text_size": "normal",
    }


def plain_text_element(content: str, *, text_color: str = "default") -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": _truncate(content.strip() or "（无内容）", 9000),
            "text_size": "normal",
            "text_color": text_color,
        },
    }


def build_ai_card(tool: str, model: str, result: str, thinking: str = "") -> dict[str, Any]:
    display_tool = tool.capitalize()
    title = f"{TOOL_ICONS.get(tool, '🤖')}{display_tool} {model}"
    elements = [markdown_element(result)]
    thinking_text = thinking.strip()
    if thinking_text:
        thinking_title = f"分析过程 ({len(thinking_text)}字)"
        elements.extend(
            [
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
                    "elements": [plain_text_element(thinking_text)],
                },
            ]
        )
    return _base_card(title, "blue", elements, summary=_truncate(result.replace("\n", " "), 100))


def build_error_card(title: str, message: str, details: str = "") -> dict[str, Any]:
    content = message if not details else f"{message}\n\n{details}"
    return _base_card(f"⚠️ {title}", "red", [plain_text_element(content, text_color="red")], summary=message[:100])


def build_generic_card(title: str, content: str, template: str = "blue") -> dict[str, Any]:
    return _base_card(title, template, [markdown_element(content)], summary=content.replace("\n", " ")[:100])


def build_help_card(commands: list[tuple[str, str]]) -> dict[str, Any]:
    lines = ["**支持的操作**", ""]
    for command, desc in commands:
        lines.append(f"- `{command}`：{desc}")
    return build_generic_card("🤖 FeishuBot 帮助", "\n".join(lines), "blue")
