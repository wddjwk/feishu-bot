from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from services.ai_runner import AIResult

_TOOL_SECTION_RE = re.compile(
    r"^(📎 Tool (?:result|partial)[^\n]*\n)(.*?)(\n*(?=^[🧠🛠📎📚]|\Z))",
    re.DOTALL | re.MULTILINE,
)


def shape_for_display(result: AIResult, config: Any) -> AIResult:
    """在 parser 产出完整内容后、渲染卡片前，对结果做展示层变换。

    parser 只负责解析原始内容（含完整的工具结果），所有面向展示的裁剪/改写都集中在此，
    后续新增展示逻辑只需在此追加调用。
    """
    result = _truncate_tool_results(result, config)
    return result


def _truncate_tool_results(result: AIResult, config: Any) -> AIResult:
    limit = _tool_result_limit(config)
    if limit <= 0 or not result.thinking:
        return result
    shaped = _TOOL_SECTION_RE.sub(
        lambda m: m.group(1) + _truncate_text(m.group(2), limit) + m.group(3),
        result.thinking,
    )
    if shaped == result.thinking:
        return result
    return replace(result, thinking=shaped)


def _tool_result_limit(config: Any) -> int:
    get = getattr(config, "get", None)
    value = get("options.features.show_max_tool_result_length", 0) if callable(get) else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"
