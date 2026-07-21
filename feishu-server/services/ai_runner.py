from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Config, ConfigError


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


@dataclass
class ActiveProcessInfo:
    message_id: str
    pid: int
    tool: str
    model: str
    started_at: float


@dataclass(frozen=True)
class ToolSpec:
    name: str
    command: str
    base_args: tuple[str, ...]
    session_args: tuple[str, ...]
    resume_args: tuple[str, ...]
    prompt_transport: str
    output_parser: str
    env: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(cls, name: str, config: Config) -> "ToolSpec":
        raw = config.tool_config(name)
        command = str(raw.get("command") or name).strip()
        if not command:
            raise ConfigError(f"AI 工具 {name} 缺少 command")
        base_args = cls._args(raw.get("base_args", raw.get("args", [])), name, "base_args")
        session_args = cls._args(raw.get("session_args", []), name, "session_args")
        resume_args = cls._args(raw.get("resume_args", []), name, "resume_args")
        transport = raw.get("prompt_transport")
        if transport is None:
            transport = "stdin" if bool(raw.get("stdin", True)) else "argument"
        prompt_transport = str(transport)
        if prompt_transport not in {"stdin", "argument"}:
            raise ConfigError(f"AI 工具 {name} 的 prompt_transport 必须为 stdin 或 argument")
        parser = raw.get("output_parser")
        if parser is None:
            parser = "copilot_json" if name == "copilot" else "stream_json"
        output_parser = str(parser)
        if output_parser not in {"stream_json", "copilot_json"}:
            raise ConfigError(f"AI 工具 {name} 使用了不支持的 output_parser：{output_parser}")
        model_environment = getattr(config, "model_environment", None)
        env = model_environment(name) if callable(model_environment) else {}
        if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            raise ConfigError(f"AI 工具 {name} 的模型环境变量无效")
        return cls(name, command, base_args, session_args, resume_args, prompt_transport, output_parser, tuple(sorted(env.items())))

    def build_command(self, model: str, session_id: str | None, resume_session_id: str | None) -> list[str]:
        values = {"model": model, "session_id": resume_session_id or session_id or ""}
        args = self._render(self.base_args, values)
        if resume_session_id:
            if not self.resume_args:
                raise ConfigError(f"AI 工具 {self.name} 未配置 resume_args，无法续接会话")
            args.extend(self._render(self.resume_args, values))
        elif session_id:
            if not self.session_args:
                raise ConfigError(f"AI 工具 {self.name} 未配置 session_args，无法创建指定会话")
            args.extend(self._render(self.session_args, values))
        return [self.command, *args]

    @staticmethod
    def _args(value: Any, tool: str, field: str) -> tuple[str, ...]:
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(f"AI 工具 {tool} 的 {field} 必须是字符串数组")
        return tuple(value)

    @staticmethod
    def _render(args: tuple[str, ...], values: dict[str, str]) -> list[str]:
        try:
            return [arg.format(**values) for arg in args]
        except (IndexError, KeyError, ValueError) as exc:
            raise ConfigError(f"AI 命令参数模板无效：{exc}") from exc


class ActiveProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._info: dict[str, ActiveProcessInfo] = {}

    def register(self, message_id: str, process: subprocess.Popen[str], *, tool: str, model: str) -> None:
        with self._lock:
            self._processes[message_id] = process
            self._info[message_id] = ActiveProcessInfo(
                message_id=message_id,
                pid=process.pid,
                tool=tool,
                model=model,
                started_at=time.time(),
            )

    def unregister(self, message_id: str) -> None:
        with self._lock:
            self._processes.pop(message_id, None)
            self._info.pop(message_id, None)

    def running(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            items = []
            for message_id, info in self._info.items():
                process = self._processes.get(message_id)
                if not process or process.poll() is not None:
                    continue
                items.append(
                    {
                        "message_id": info.message_id,
                        "pid": info.pid,
                        "tool": info.tool,
                        "model": info.model,
                        "elapsed_seconds": int(now - info.started_at),
                    }
                )
            return items

    def kill(self, identifier: str) -> str | None:
        with self._lock:
            message_id = self._resolve_locked(identifier)
            process = self._processes.get(message_id or "")
        if not process or process.poll() is not None:
            return None
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return None
        return message_id

    def _resolve_locked(self, identifier: str) -> str | None:
        if identifier in self._processes:
            return identifier
        matches = [message_id for message_id in self._processes if message_id.startswith(identifier)]
        return matches[0] if len(matches) == 1 else None


class AIRunner:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.registry = ActiveProcessRegistry()

    def kill(self, identifier: str) -> str | None:
        message_id = self.registry.kill(identifier)
        if message_id:
            logger.info("已向 AI 进程发送终止信号：消息=%s", message_id)
        else:
            logger.warning("未找到可终止的 AI 进程：标识=%s", identifier)
        return message_id

    def running(self) -> list[dict[str, Any]]:
        return self.registry.running()

    def supports_explicit_session(self, tool: str | None = None) -> bool:
        selected_tool = tool or self.config.current_tool()
        return bool(ToolSpec.from_config(selected_tool, self.config).session_args)

    def run(
        self,
        prompt: dict[str, Any] | str,
        message_id: str,
        *,
        tool: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        resume_session_id: str | None = None,
        timeout_seconds: int | None = None,
        register: bool = True,
    ) -> AIResult:
        try:
            selected_tool = tool or self.config.current_tool()
            selected_model = model or self.config.resolve_model(selected_tool)
            if session_id and resume_session_id:
                return AIResult(
                    False,
                    selected_tool,
                    selected_model,
                    error="一次 AI 执行不能同时创建会话和续接会话",
                    usage=TokenUsage(),
                )
            spec = ToolSpec.from_config(selected_tool, self.config)
            command = spec.build_command(selected_model, session_id, resume_session_id)
            timeout = int(timeout_seconds if timeout_seconds is not None else self.config.get("ai.timeout_seconds", 600))
            if timeout <= 0:
                raise ValueError("AI 超时时间必须大于 0 秒")
        except (ConfigError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.error("无法准备 AI 执行：%s", exc)
            return AIResult(False, tool or "", model or "", error=str(exc), usage=TokenUsage())
        prompt_text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False, indent=2)
        if spec.prompt_transport == "argument":
            max_argument_bytes = int(self.config.get("ai.max_argument_prompt_bytes", 100_000))
            prompt_bytes = len(prompt_text.encode("utf-8"))
            if max_argument_bytes <= 0:
                error = "ai.max_argument_prompt_bytes 必须大于 0"
                logger.error("无法准备 AI 执行：%s", error)
                return AIResult(False, selected_tool, selected_model, error=error, usage=TokenUsage())
            if prompt_bytes > max_argument_bytes:
                error = f"AI 提示词为 {prompt_bytes} 字节，超过参数输入上限 {max_argument_bytes} 字节"
                logger.error("未执行 AI 命令：消息=%s 原因=%s", message_id, error)
                return AIResult(False, selected_tool, selected_model, error=error, usage=TokenUsage())
            command.append(prompt_text)

        workspace = self.config.resolve_path("ai.workspace").resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        logger.info("AI 执行命令：%s", shlex.join(command))
        if spec.prompt_transport == "stdin":
            logger.info("AI 标准输入 prompt：\n%s", prompt_text or "（空）")
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                stdin=subprocess.PIPE if spec.prompt_transport == "stdin" else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid,
                env={
                    **os.environ,
                    **dict(spec.env),
                },
            )
        except FileNotFoundError:
            error = f"找不到 AI 命令：{command[0]}"
            logger.error("%s", error)
            return AIResult(False, selected_tool, selected_model, error=error, usage=TokenUsage())
        except OSError as exc:
            error = f"启动 AI 进程失败：{exc}"
            logger.exception("%s", error)
            return AIResult(False, selected_tool, selected_model, error=error, usage=TokenUsage())

        if register:
            self.registry.register(message_id, process, tool=selected_tool, model=selected_model)
        try:
            stdout, stderr = process.communicate(input=prompt_text if spec.prompt_transport == "stdin" else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            partial = parse_output(spec.output_parser, stdout, stderr)
            self._log_ai_output(partial.result, stderr, completed=False)
            elapsed = time.monotonic() - started_at
            logger.error("AI 进程超时：消息=%s 耗时=%.2fs 超时=%ss", message_id, elapsed, timeout)
            return AIResult(
                False,
                selected_tool,
                selected_model,
                thinking=stdout,
                stderr=stderr,
                exit_code=process.returncode,
                error=f"AI 进程在 {timeout} 秒后超时",
                usage=TokenUsage(),
                session_id=resume_session_id or session_id,
            )
        finally:
            if register:
                self.registry.unregister(message_id)

        parsed = parse_output(spec.output_parser, stdout, stderr)
        parsed.tool = selected_tool
        parsed.model = selected_model
        parsed.exit_code = process.returncode
        parsed.stdout = stdout
        parsed.stderr = stderr
        parsed.session_id = resume_session_id or session_id or parsed.session_id
        if process.returncode != 0 and not parsed.result:
            parsed.ok = False
            parsed.error = stderr.strip() or f"AI 进程退出码异常：{process.returncode}"
        self._log_ai_output(parsed.result, stderr, completed=True)
        return parsed

    def _build_command(
        self,
        tool: str,
        model: str,
        session_id: str | None,
        resume_session_id: str | None,
    ) -> list[str]:
        return ToolSpec.from_config(tool, self.config).build_command(model, session_id, resume_session_id)

    @staticmethod
    def _log_ai_output(result: str, stderr: str, *, completed: bool) -> None:
        if result.strip():
            logger.info("AI 回复：\n%s", result.strip())
        else:
            logger.info("AI 已%s（未解析到可展示回复）", "完成" if completed else "停止")
        if stderr.strip():
            logger.warning("AI 错误输出：\n%s", stderr.strip())

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            return


def parse_output(parser: str, stdout: str, stderr: str = "") -> AIResult:
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

        if parser in {"copilot", "copilot_json"}:
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
        tool=parser,
        model="",
        thinking=thinking,
        result=result,
        usage=usage,
        session_id=session_id,
        error="\n".join(error_parts).strip() if saw_error else ("" if result else "AI 输出中未找到最终回复"),
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
