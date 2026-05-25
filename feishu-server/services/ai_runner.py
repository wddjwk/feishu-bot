from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config


logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class AIResult:
    ok: bool
    tool: str
    model: str
    thinking: str = ""
    result: str = ""
    usage: TokenUsage | None = None
    session_id: str | None = None
    exit_code: int | None = None
    error: str = ""
    stdout: str = ""
    stderr: str = ""


class ActiveProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def register(self, message_id: str, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes[message_id] = process

    def unregister(self, message_id: str) -> None:
        with self._lock:
            self._processes.pop(message_id, None)

    def kill(self, message_id: str) -> bool:
        with self._lock:
            process = self._processes.get(message_id)
        if not process or process.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True


class AIRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = ActiveProcessRegistry()

    def kill(self, message_id: str) -> bool:
        return self.registry.kill(message_id)

    def run(
        self,
        prompt: dict[str, Any] | str,
        message_id: str,
        *,
        tool: str | None = None,
        model: str | None = None,
        resume_session_id: str | None = None,
    ) -> AIResult:
        selected_tool = tool or self.config.current_tool()
        selected_model = model or self.config.resolve_model(selected_tool)
        prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False, indent=2)
        try:
            command = self._build_command(selected_tool, selected_model, resume_session_id)
        except Exception as exc:
            return AIResult(False, selected_tool, selected_model, error=str(exc), usage=TokenUsage())

        workspace = self.config.resolve_path("ai.workspace")
        workspace.mkdir(parents=True, exist_ok=True)
        timeout = int(self.config.get("ai.timeout_seconds", 600))
        logger.info("running AI tool=%s model=%s message_id=%s cwd=%s", selected_tool, selected_model, message_id, workspace)
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid,
            )
        except FileNotFoundError:
            return AIResult(False, selected_tool, selected_model, error=f"command not found: {command[0]}", usage=TokenUsage())
        except OSError as exc:
            return AIResult(False, selected_tool, selected_model, error=f"failed to start AI process: {exc}", usage=TokenUsage())

        self.registry.register(message_id, process)
        try:
            stdout, stderr = process.communicate(input=prompt_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            return AIResult(
                False,
                selected_tool,
                selected_model,
                thinking=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                error=f"AI process timed out after {timeout}s",
                usage=TokenUsage(),
            )
        finally:
            self.registry.unregister(message_id)

        parsed = parse_output(selected_tool, stdout, stderr)
        parsed.tool = selected_tool
        parsed.model = selected_model
        parsed.exit_code = process.returncode
        parsed.stdout = stdout
        parsed.stderr = stderr
        if process.returncode != 0 and not parsed.result:
            parsed.ok = False
            parsed.error = stderr.strip() or f"AI process exited with code {process.returncode}"
        return parsed

    def _build_command(self, tool: str, model: str, resume_session_id: str | None) -> list[str]:
        tool_cfg = self.config.tool_config(tool)
        command = str(tool_cfg.get("command") or tool)
        values = {"model": model, "session_id": resume_session_id or ""}
        args = [str(arg).format(**values) for arg in tool_cfg.get("args", [])]
        if resume_session_id:
            args.extend(str(arg).format(**values) for arg in tool_cfg.get("resume_args", []))
        return [command, *args]

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            return


def parse_output(tool: str, stdout: str, stderr: str = "") -> AIResult:
    usage = TokenUsage()
    session_id: str | None = None
    thinking_parts: list[str] = []
    result_parts: list[str] = []
    content_parts: list[str] = []
    content_delta_parts: list[str] = []
    reasoning_delta_parts: list[str] = []
    reasoning_final_parts: list[str] = []
    error_parts: list[str] = []
    saw_error = False

    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            content_parts.append(line)
            continue
        if not isinstance(event, dict):
            continue
        _merge_usage(usage, _extract_usage(event))
        session_id = session_id or _extract_session_id(event)
        event_type = str(event.get("type") or event.get("event") or event.get("kind") or "").lower()
        if event.get("is_error") is True or str(event.get("subtype", "")).startswith("error"):
            saw_error = True
            error_text = _extract_text(event.get("errors") or event.get("error") or event)
            if error_text:
                error_parts.append(error_text)

        process_text = _format_process_event(event, event_type)
        if process_text:
            thinking_parts.append(process_text)

        if tool == "copilot":
            if event_type == "reasoning_delta":
                text = _extract_text(event.get("delta") or event)
                if text:
                    reasoning_delta_parts.append(text)
                continue
            if event_type.endswith("content_delta") or event_type.endswith("answer_delta") or event_type.endswith("message_delta"):
                text = _extract_text(event.get("delta") or event)
                if text:
                    content_delta_parts.append(text)
                continue
            if event_type == "assistant.message":
                text = _extract_text(event)
                if text:
                    result_parts.append(text)
                continue
            if event_type == "reasoning":
                text = _extract_text(event)
                if text:
                    reasoning_final_parts.append(text)
                continue

        if event_type == "assistant":
            text = _extract_assistant_text(event)
            if text:
                content_parts.append(text)
            continue

        text = _extract_text(event)
        if not text:
            continue
        if event_type == "result" or "final" in event_type or "completion" in event_type:
            result_parts.append(text)
        elif "assistant" in event_type or "message" in event_type:
            content_parts.append(text)
        elif ("tool" in event_type or "reasoning" in event_type) and not process_text:
            thinking_parts.append(text)
        else:
            content_parts.append(text)

    if reasoning_delta_parts:
        thinking_parts.extend(reasoning_delta_parts)
    elif reasoning_final_parts:
        thinking_parts.extend(reasoning_final_parts)

    _merge_usage(usage, _usage_from_stderr(stderr))
    if content_delta_parts and not result_parts:
        result_parts.append("".join(content_delta_parts).strip())
    result = "\n".join(part for part in result_parts if part).strip()
    if not result:
        result = _fallback_result(content_parts or thinking_parts)
    thinking = "\n".join(part for part in thinking_parts if part).strip()
    return AIResult(
        ok=bool(result) and not saw_error,
        tool=tool,
        model="",
        thinking=thinking,
        result=result,
        usage=usage,
        session_id=session_id,
        error="\n".join(error_parts).strip() if saw_error else ("" if result else "AI output did not contain a result"),
    )


def _fallback_result(parts: list[str]) -> str:
    for part in reversed(parts):
        cleaned = part.strip()
        if cleaned:
            return cleaned
    return ""


def _format_process_event(event: dict[str, Any], event_type: str) -> str:
    parts: list[str] = []
    content = _event_content_blocks(event)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or block.get("tag") or block.get("kind") or "").lower()
            if block_type in {"thinking", "reasoning", "reasoning_delta"}:
                text = _extract_text(block.get("thinking") or block.get("text") or block.get("content") or block)
                if text:
                    parts.append(f"🧠 Thinking\n{text}")
            elif block_type in {"tool_use", "tool_call", "server_tool_use"}:
                parts.append(_format_tool_use(block))
            elif block_type in {"tool_result", "tool_response"}:
                text = _extract_text(block.get("content") or block.get("result") or block)
                if text:
                    parts.append(f"📎 Tool result\n{_trim(text)}")
    if not parts and "tool" in event_type:
        if "result" in event_type or "response" in event_type:
            text = _extract_text(event.get("data") or event.get("result") or event)
            if text:
                parts.append(f"📎 Tool result\n{_trim(text)}")
        else:
            parts.append(_format_tool_use(event.get("data") if isinstance(event.get("data"), dict) else event))
    return "\n\n".join(part for part in parts if part)


def _event_content_blocks(event: dict[str, Any]) -> Any:
    message = event.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    data = event.get("data")
    if isinstance(data, dict) and "content" in data:
        return data.get("content")
    return event.get("content")


def _format_tool_use(block: dict[str, Any]) -> str:
    name = str(block.get("name") or block.get("tool_name") or block.get("tool") or block.get("function") or "tool")
    payload = block.get("input") or block.get("arguments") or block.get("args") or block.get("parameters")
    if name.lower() in {"skill", "skills"}:
        skill_name = ""
        if isinstance(payload, dict):
            skill_name = str(payload.get("skill") or payload.get("name") or "")
        return f"📚 Skill {skill_name}".strip()
    detail = _compact_json(payload)
    return f"🛠 Tool {name}" + (f"\n{detail}" if detail else "")


def _extract_assistant_text(event: dict[str, Any]) -> str:
    content = _event_content_blocks(event)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                text = _extract_text(block)
                if text:
                    parts.append(text)
                continue
            block_type = str(block.get("type") or block.get("tag") or "").lower()
            if block_type in {"text", "markdown"}:
                text = _extract_text(block.get("text") or block.get("content") or block)
                if text:
                    parts.append(text)
        return "\n".join(parts)
    return _extract_text(content)


def _compact_json(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return _trim(value)
    try:
        return _trim(json.dumps(value, ensure_ascii=False, sort_keys=True))
    except TypeError:
        return _trim(str(value))


def _trim(value: str, limit: int = 1200) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]"


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_text(item) for item in value)))
    if not isinstance(value, dict):
        return ""

    for key in ("result", "text", "delta", "deltaContent", "content", "summary"):
        if key in value:
            text = _extract_text(value[key])
            if text:
                return text

    data = value.get("data")
    if isinstance(data, dict):
        text = _extract_text(data)
        if text:
            return text

    message = value.get("message")
    if isinstance(message, dict):
        text = _extract_text(message.get("content") or message)
        if text:
            return text
    if isinstance(message, str):
        return message

    choices = value.get("choices")
    if isinstance(choices, list):
        return _extract_text(choices)

    return ""


def _extract_usage(event: dict[str, Any]) -> TokenUsage:
    usage = TokenUsage()
    candidates: list[dict[str, Any]] = []
    stack: list[Any] = [event]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if any(
                key in current
                for key in (
                    "input_tokens",
                    "prompt_tokens",
                    "output_tokens",
                    "completion_tokens",
                    "inputTokens",
                    "outputTokens",
                    "cacheReadInputTokens",
                )
            ):
                candidates.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    for item in candidates:
        usage.input_tokens = _first_int(item, ("input_tokens", "prompt_tokens", "inputTokens"), usage.input_tokens)
        usage.cached_tokens = _first_int(
            item,
            ("cached_tokens", "cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens"),
            usage.cached_tokens,
        )
        usage.output_tokens = _first_int(item, ("output_tokens", "completion_tokens", "outputTokens"), usage.output_tokens)
    return usage


def _extract_session_id(event: dict[str, Any]) -> str | None:
    stack: list[Any] = [event]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("session_id", "sessionId", "conversation_id", "conversationId"):
                value = current.get(key)
                if isinstance(value, str) and value:
                    return value
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return None


def _first_int(source: dict[str, Any], keys: tuple[str, ...], current: int | None) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return current


def _merge_usage(target: TokenUsage, source: TokenUsage) -> None:
    if source.input_tokens is not None:
        target.input_tokens = source.input_tokens
    if source.cached_tokens is not None:
        target.cached_tokens = source.cached_tokens
    if source.output_tokens is not None:
        target.output_tokens = source.output_tokens


def _usage_from_stderr(stderr: str) -> TokenUsage:
    usage = TokenUsage()
    patterns = {
        "input_tokens": r"(?:input|prompt)[^\d]{0,20}(\d+)\s*tokens?",
        "cached_tokens": r"cached[^\d]{0,20}(\d+)\s*tokens?",
        "output_tokens": r"(?:output|completion)[^\d]{0,20}(\d+)\s*tokens?",
    }
    for attr, pattern in patterns.items():
        match = re.search(pattern, stderr, flags=re.IGNORECASE)
        if match:
            setattr(usage, attr, int(match.group(1)))
    return usage
