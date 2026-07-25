from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import Config, ConfigError
import logger


@dataclass
class TokenUsage:
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class ThoughtPart:
    """parser 产出的标准化结构化片段；由中间层 format_thinking 渲染成展示字符串。"""

    kind: str  # "thinking" | "tool_use" | "tool_result" | "tool_partial" | "text"
    text: str = ""
    name: str = ""  # tool_use: 工具名
    args: Any = None  # tool_use: 原始参数
    is_error: bool = False  # tool_result


@dataclass
class AIResult:
    ok: bool
    tool: str
    model: str
    parts: list[ThoughtPart] = field(default_factory=list)
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
        base_args = cls._args(raw.get("base_args", []), name, "base_args")
        session_args = cls._args(raw.get("session_args", []), name, "session_args")
        resume_args = cls._args(raw.get("resume_args", []), name, "resume_args")
        prompt_transport = str(raw.get("prompt_transport") or "stdin")
        if prompt_transport not in {"stdin", "argument"}:
            raise ConfigError(f"AI 工具 {name} 的 prompt_transport 必须为 stdin 或 argument")
        output_parser = str(raw.get("output_parser") or "claude-stream-json")
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
            max_argument_bytes = int(self.config.get("ai.max_prompt_length_bytes", 100_000))
            prompt_bytes = len(prompt_text.encode("utf-8"))
            if max_argument_bytes <= 0:
                error = "ai.max_prompt_length_bytes 必须大于 0"
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
        elif os.name == "nt":
            logger.warning(
                "Windows 下使用 argument 方式传递 prompt（%s 字节），多行 JSON 可能被 cmd.exe 破坏",
                len(prompt_text.encode("utf-8")),
            )
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
            self._log_ai_output(partial.result, stderr, ok=False)
            elapsed = time.monotonic() - started_at
            logger.error("AI 进程超时：消息=%s 耗时=%.2fs 超时=%ss", message_id, elapsed, timeout)
            return AIResult(
                False,
                selected_tool,
                selected_model,
                parts=[ThoughtPart("text", text=stdout)] if stdout else [],
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
        elapsed = time.monotonic() - started_at
        logger.info(
            "AI 进程已结束：消息=%s 退出码=%s 耗时=%.2fs 会话=%s token(输入=%s 缓存=%s 输出=%s)",
            message_id,
            process.returncode,
            elapsed,
            parsed.session_id or "无",
            parsed.usage.input_tokens if parsed.usage else None,
            parsed.usage.cached_tokens if parsed.usage else None,
            parsed.usage.output_tokens if parsed.usage else None,
        )
        self._log_ai_output(parsed.result, stderr, ok=parsed.ok)
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
    def _log_ai_output(result: str, stderr: str, *, ok: bool) -> None:
        if ok:
            logger.info("AI 执行成功")
            if result.strip():
                logger.info("AI 回复：\n%s", result.strip())
            if stderr.strip():
                logger.info("AI stderr：\n%s", stderr.strip())
        else:
            logger.error(
                "AI 执行失败\n── stderr ──\n%s\n── result ──\n%s",
                stderr or "（空）",
                result or "（空）",
            )

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            return


def parse_output(parser: str, stdout: str, stderr: str = "") -> AIResult:
    """Dispatch stdout parsing to a format-specific parser class.

    Use :func:`get_parser` to obtain a parser by name. The parser registry is
    populated automatically by :class:`BaseOutputParser` subclasses.
    """
    return get_parser(parser).parse(stdout, stderr)


def get_parser(name: str) -> "BaseOutputParser":
    """按 format name 获取解析器实例。注册表由 BaseOutputParser 子类自动填充。"""
    cls = BaseOutputParser._registry.get(name)
    if cls is None:
        raise ValueError(f"不支持的 output_parser：{name}（已注册：{sorted(BaseOutputParser._registry)})")
    return cls()


class BaseOutputParser:
    """CLI JSON 流式输出的解析器基类。

    子类通过 :attr:`name` 声明自己处理的格式名，并在定义时自动注册到 ``_registry``。
    这样 ``parse_output`` 的分发表完全由 parser 类自身维护：新增 CLI 时只需新增一个
    parser 类，无需修改 ToolSpec 或任何分发逻辑。

    若某个 CLI 与现有 CLI 使用相同的 JSON 协议，可创建子类并设置 ``inherits`` 指向
    父类的 ``name``。这样的派生类复用父类实现、不注册，作为命名占位以便未来协议分化。

    子类只需实现 :meth:`_handle_event`，在其中按事件类型精确提取 usage / session_id /
    内容块；公共的 error 检测、收尾合并与结果拼装由基类完成。
    """

    name: str = ""
    inherits: str = ""
    _registry: "dict[str, type[BaseOutputParser]]" = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.name and not cls.inherits:
            BaseOutputParser._registry[cls.name] = cls

    def parse(self, stdout: str, stderr: str) -> AIResult:
        ctx = _ParseContext()
        for event in _iter_events(stdout):
            self._collect_common(ctx, event)
            event_type = _event_type(event)
            if "__plain_text__" in event:
                continue
            self._handle_event(ctx, event, event_type)
        result = self._finalize(ctx, stderr)
        result.stdout = stdout
        result.stderr = stderr
        return result

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        raise NotImplementedError

    @staticmethod
    def _collect_common(ctx: "_ParseContext", event: dict[str, Any]) -> None:
        """每个事件都应执行的公共逻辑：plain text 兜底和错误检测。

        usage 和 session_id 的提取由各 parser 在 _handle_event 中按事件类型精确处理，
        避免对每个事件做全量 DFS（Pi 有 829 个事件，绝大多数不含 usage 数据）。
        """
        if "__plain_text__" in event:
            ctx.content_parts.append(event["__plain_text__"])
            return
        if event.get("is_error") is True or str(event.get("subtype", "")).startswith("error"):
            ctx.saw_error = True
            error_text = _extract_text(event.get("errors") or event.get("error") or event)
            if error_text:
                ctx.error_parts.append(error_text)

    def _finalize(self, ctx: "_ParseContext", stderr: str) -> AIResult:
        if ctx.reasoning_delta_parts:
            ctx.thinking_parts.extend(ThoughtPart("thinking", text=t) for t in ctx.reasoning_delta_parts)
        elif ctx.reasoning_final_parts:
            ctx.thinking_parts.extend(ThoughtPart("thinking", text=t) for t in ctx.reasoning_final_parts)
        _merge_usage(ctx.usage, _usage_from_stderr(stderr))
        if ctx.content_delta_parts and not ctx.result_parts:
            ctx.result_parts.append("".join(ctx.content_delta_parts).strip())
        result = "\n".join(part for part in ctx.result_parts if part).strip()
        if not result:
            result = _fallback_result(ctx.content_parts or ctx.thinking_parts)
        return AIResult(
            ok=bool(result) and not ctx.saw_error,
            tool=self.name,
            model="",
            parts=list(ctx.thinking_parts),
            result=result,
            usage=ctx.usage,
            session_id=ctx.session_id,
            error=(
                "\n".join(ctx.error_parts).strip()
                if ctx.saw_error
                else ("" if result else "AI 输出中未找到最终回复")
            ),
        )


class ClaudeParser(BaseOutputParser):
    """解析 Claude / Codebuddy 系列的 stream-json 输出。

    事件协议：
      - type=system (subtype=init)：初始化事件，提取 session_id
      - type=system (subtype=hook_*|thinking_tokens)：生命周期/进度事件，忽略
      - type=assistant：message.content[] 包含 thinking 块（→思考）和 text/markdown 块（→正文）
      - type=result (subtype=success|error_during_execution)：result 字段为权威最终回复
      - type=tool_use / tool_result：工具调用过程事件，格式化到思考流
    """

    name = "claude-stream-json"

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        if event_type == "system":
            if str(event.get("subtype") or "").lower() == "init":
                sid = event.get("session_id")
                if isinstance(sid, str) and sid:
                    ctx.session_id = ctx.session_id or sid
            return
        if event_type == "assistant":
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                ctx.session_id = ctx.session_id or sid
            process_parts, body_text = _split_assistant_content(event)
            ctx.thinking_parts.extend(process_parts)
            if body_text:
                ctx.content_parts.append(_strip_inline_thinking(ctx, body_text))
            return
        if _dispatch_process_event(ctx, event, event_type):
            return
        if event_type == "result":
            _merge_usage(ctx.usage, _extract_usage(event))
            sid = event.get("session_id")
            if isinstance(sid, str) and sid:
                ctx.session_id = ctx.session_id or sid
        _dispatch_result(ctx, event, event_type)


class QoderCliParser(ClaudeParser):
    """解析 QoderCLI 的 stream-json 输出。

    协议与 Claude CLI 完全相同，因此通过 ``inherits`` 复用 ClaudeParser 实现。
    保留为独立子类以便未来 QoderCLI 引入协议差异时可以单独演化。
    """

    name = "qodercli-stream-json"
    inherits = "claude-stream-json"


class CodeBuddyParser(ClaudeParser):
    """解析 CodeBuddy CLI 的 stream-json 输出。

    协议与 Claude CLI 完全相同，因此通过 ``inherits`` 复用 ClaudeParser 实现。
    保留为独立子类以便未来 CodeBuddy 引入协议差异时可以单独演化。
    """

    name = "codebuddy-stream-json"
    inherits = "claude-stream-json"


class PiParser(BaseOutputParser):
    """解析 Pi CLI 的 session/turn/message_update 流式输出。

    工具调用与结果按 call id 配对：toolcall_end 携带 toolCall.id，tool_execution_*
    携带同一 toolCallId。同一轮的调用先缓存，结果到达时挂回对应调用，turn_end 时按
    调用顺序输出「🛠 调用 + 📎 结果」，避免「所有调用 → 所有结果」割裂，也避免
    toolcall_end 与 tool_execution_start 重复渲染调用行（结果可能按完成顺序到达，
    必须按 id 而非到达顺序配对）。

    内容提取策略：
      - thinking / text：turn_end 的完整 content 块是权威来源，deltas 仅作兜底。
      - toolcall_end：缓存调用（id + name + arguments），不立即输出。
      - tool_execution_start：与 toolcall_end 重复，仅在缺 toolcall_end 时补建调用。
      - tool_execution_update：挂到对应调用的 partials。
      - tool_execution_end：挂到对应调用的 result；无对应调用时单独输出。
      - turn_end：提取 thinking/text 后，按调用顺序 flush 本轮缓存的调用。
    """

    name = "pi-json"

    def parse(self, stdout: str, stderr: str) -> AIResult:
        self._turn_calls: list[str] = []
        self._calls: dict[str, dict[str, Any]] = {}
        return super().parse(stdout, stderr)

    def _reset_turn(self) -> None:
        self._turn_calls = []
        self._calls = {}

    def _remember_call(self, call_id: str, call_text: str) -> None:
        if not call_id:
            return
        if call_id not in self._calls:
            self._calls[call_id] = {"call": call_text, "partials": [], "result": None}
            self._turn_calls.append(call_id)
        elif call_text:
            self._calls[call_id]["call"] = call_text

    def _flush_turn(self, ctx: "_ParseContext") -> None:
        for call_id in self._turn_calls:
            entry = self._calls.get(call_id)
            if not entry:
                continue
            if entry.get("call"):
                ctx.thinking_parts.append(entry["call"])
            for partial in entry.get("partials", []):
                ctx.thinking_parts.append(partial)
            if entry.get("result"):
                ctx.thinking_parts.append(entry["result"])
        self._reset_turn()

    def _finalize(self, ctx: "_ParseContext", stderr: str) -> AIResult:
        self._flush_turn(ctx)
        return super()._finalize(ctx, stderr)

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        if event_type == "session":
            sid = event.get("id")
            if isinstance(sid, str) and sid:
                ctx.session_id = ctx.session_id or sid
            return
        if event_type == "turn_start":
            self._reset_turn()
            return
        if event_type == "message_update":
            inner = event.get("assistantMessageEvent")
            if not isinstance(inner, dict):
                return
            inner_type = str(inner.get("type") or "").lower()
            if inner_type == "toolcall_end":
                call_id, call_text = _pi_toolcall(inner)
                if call_text:
                    self._remember_call(call_id, call_text)
            elif inner_type == "text_delta":
                delta = inner.get("delta")
                if isinstance(delta, str) and delta:
                    ctx.content_delta_parts.append(_strip_inline_thinking(ctx, delta))
            return
        if event_type == "tool_execution_start":
            call_id = str(event.get("toolCallId") or "")
            if call_id and call_id not in self._calls:
                self._remember_call(call_id, _format_pi_tool_execution(event))
            return
        if event_type == "tool_execution_end":
            call_id = str(event.get("toolCallId") or "")
            result_text = _format_pi_tool_result(event)
            if call_id and call_id in self._calls:
                if result_text:
                    self._calls[call_id]["result"] = result_text
            elif result_text:
                ctx.thinking_parts.append(result_text)
            return
        if event_type == "tool_execution_update":
            call_id = str(event.get("toolCallId") or "")
            partial_text = _format_pi_tool_partial(event)
            if call_id and call_id in self._calls and partial_text:
                self._calls[call_id]["partials"].append(partial_text)
            return
        if event_type == "turn_end":
            message = event.get("message")
            if isinstance(message, dict):
                _accumulate_pi_usage(ctx.usage, message.get("usage"))
                is_final = str(message.get("stopReason") or "").lower() == "stop"
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = str(block.get("type") or "").lower()
                        if block_type == "thinking":
                            text = block.get("thinking") or block.get("text") or ""
                            if text:
                                ctx.thinking_parts.append(ThoughtPart("thinking", text=text))
                        elif block_type == "text":
                            text = block.get("text") or ""
                            if text:
                                clean = _strip_inline_thinking(ctx, text)
                                if clean:
                                    if is_final:
                                        ctx.result_parts.append(clean)
                                    else:
                                        ctx.thinking_parts.append(ThoughtPart("text", text=clean))
            self._flush_turn(ctx)
            return
        # agent_end / agent_settled / message_start / message_end: no-op


class CopilotParser(BaseOutputParser):
    """解析 Microsoft Copilot CLI 的流式输出（基于真实 capture 验证）。

    事件类型均为点分前缀：assistant.* / tool.* / session.* / user.* / mcp.*。

    事件协议：
      - assistant.message_start / assistant.turn_start / assistant.turn_end / assistant.idle：结构
      - assistant.reasoning：推理事件。可读 content 通常为空（推理存于 reasoningOpaque），忽略
      - assistant.message_delta：增量正文片段，位于 data.deltaContent
      - assistant.message：完整一轮输出。data.content 为正文；data.toolRequests[] 为工具调用；
        data.outputTokens 为输出 token 数
      - assistant.tool_call_delta：工具调用流。data.{toolCallId,toolName,inputDelta,type}
      - tool.execution_start / tool.execution_complete / tool.execution_partial_result：
        顶层工具执行。data.{toolName,arguments} + data.result/data.error
      - session.* / user.message / mcp.*：结构事件，no-op
      - result：终态事件。提取 sessionId；无 result 字段，usage 不含 token 数

    正文来源：所有 assistant.message 的非空 data.content 拼合为正文。
    """

    name = "copilot-json"

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        # assistant.message_delta: incremental body (data.deltaContent)
        if event_type == "assistant.message_delta":
            data = event.get("data")
            if isinstance(data, dict):
                delta = data.get("deltaContent")
                if isinstance(delta, str) and delta:
                    ctx.content_delta_parts.append(delta)
            return
        # assistant.message: authoritative turn output (body + tool requests + output tokens)
        if event_type == "assistant.message":
            data = event.get("data")
            if isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, str) and content.strip():
                    ctx.result_parts.append(content)
                reqs = data.get("toolRequests")
                if isinstance(reqs, list):
                    for req in reqs:
                        if isinstance(req, dict):
                            ctx.thinking_parts.append(_format_copilot_tool_request(req))
                out_tokens = data.get("outputTokens")
                if isinstance(out_tokens, int) and out_tokens > 0:
                    ctx.usage.output_tokens = (ctx.usage.output_tokens or 0) + out_tokens
            return
        # assistant.tool_call_delta: streaming partial tool requests. The same tool
        # requests appear in assistant.message.toolRequests[] with complete arguments
        # (two forms of the same data — keep the complete form, skip the stream).
        if event_type == "assistant.tool_call_delta":
            return
        # assistant.reasoning: content is usually empty (real reasoning lives in the
        # encrypted reasoningOpaque field). Render only when there's actual content;
        # emitting an empty "🧠 Thinking" line for empty content is noise.
        if event_type == "assistant.reasoning":
            data = event.get("data")
            if isinstance(data, dict):
                text = data.get("content")
                if isinstance(text, str) and text.strip():
                    ctx.reasoning_final_parts.append(text)
            return
        # tool.execution_*: format as process event
        if event_type.startswith("tool.execution"):
            text = _format_copilot_tool_execution(event)
            if text:
                ctx.thinking_parts.append(text)
            return
        # Other (session.*, user.*, mcp.*, assistant.idle/turn_start/turn_end, result): no-op.
        # NOTE: do NOT fall through to _dispatch_process_event — it matches on substring
        # ("tool" in event_type) and would misclassify system events like
        # mcp.tools.list_changed / session.tools_updated as tool calls.
        # Backward-compat: also accept non-prefixed event names (legacy copilot CLI?)
        if event_type == "reasoning_delta":
            text = _extract_text(event.get("delta") or event)
            if text:
                ctx.reasoning_delta_parts.append(text)
            return
        if event_type == "reasoning":
            text = _extract_text(event)
            if text:
                ctx.reasoning_final_parts.append(text)
            return
        if event_type.endswith("content_delta") or event_type.endswith("answer_delta"):
            text = _extract_text(event.get("delta") or event)
            if text:
                ctx.content_delta_parts.append(text)
            return
        if event_type == "result":
            sid = event.get("sessionId") or event.get("session_id")
            if isinstance(sid, str) and sid:
                ctx.session_id = ctx.session_id or sid
        _dispatch_result(ctx, event, event_type)


class CodexParser(BaseOutputParser):
    """解析 Codex CLI 的 --json JSONL 输出。

    事件协议：
      - thread.started：thread_id 为会话 ID
      - turn.started：结构事件，无 payload
      - item.started：命令执行开始（status=in_progress），内容与 item.completed 重复，跳过
      - item.completed：
        - type=reasoning：item.text 为模型推理/思考过程
        - type=agent_message：item.text 为 Agent 消息。一次 turn 内可能有多条
          （工具调用前的"我来查一下"、工具调用间的进度播报、最终完整回答）。
          仅最后一条作为最终回复，其余归入思考流。
        - type=command_execution：command + aggregated_output 为工具调用过程
        - type=error：item.message 为非致命警告（如模型元数据缺失），不标记 saw_error
      - turn.completed：usage 含 input_tokens / cached_input_tokens / output_tokens / reasoning_output_tokens
    """

    name = "codex-json"

    def parse(self, stdout: str, stderr: str) -> AIResult:
        self._agent_messages: list[str] = []
        return super().parse(stdout, stderr)

    @staticmethod
    def _collect_common(ctx: "_ParseContext", event: dict[str, Any]) -> None:
        if "__plain_text__" in event:
            text = event["__plain_text__"]
            if not text.startswith("Reading additional input from stdin"):
                ctx.content_parts.append(text)
            return

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        if event_type == "thread.started":
            tid = event.get("thread_id")
            if isinstance(tid, str) and tid:
                ctx.session_id = ctx.session_id or tid
            return
        if event_type == "turn.completed":
            _merge_usage(ctx.usage, _extract_usage(event))
            self._distribute_agent_messages(ctx)
            return
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                return
            item_type = str(item.get("type") or "").lower()
            if item_type == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    self._agent_messages.append(_strip_inline_thinking(ctx, text))
            elif item_type == "reasoning":
                text = item.get("text")
                if isinstance(text, str) and text:
                    ctx.thinking_parts.append(ThoughtPart("thinking", text=text))
            elif item_type == "command_execution":
                command = item.get("command") or ""
                output = item.get("aggregated_output") or ""
                exit_code = item.get("exit_code")
                is_error = exit_code is not None and exit_code != 0
                if command:
                    ctx.thinking_parts.append(ThoughtPart("command", text=str(command)))
                if output and output.strip():
                    ctx.thinking_parts.append(ThoughtPart("command_result", text=output.rstrip(), is_error=is_error))
                elif is_error:
                    ctx.thinking_parts.append(ThoughtPart("command_result", text=f"exit code: {exit_code}", is_error=True))
            elif item_type == "error":
                message = item.get("message")
                if isinstance(message, str) and message:
                    ctx.thinking_parts.append(ThoughtPart("warning", text=message))
            return
        # item.started / turn.started: no-op

    def _finalize(self, ctx: "_ParseContext", stderr: str) -> AIResult:
        if self._agent_messages and not ctx.result_parts:
            self._distribute_agent_messages(ctx)
        return super()._finalize(ctx, stderr)

    def _distribute_agent_messages(self, ctx: "_ParseContext") -> None:
        if not self._agent_messages:
            return
        ctx.result_parts.append(self._agent_messages[-1])
        for msg in self._agent_messages[:-1]:
            if msg.strip():
                ctx.thinking_parts.append(ThoughtPart("agent", text=msg))
        self._agent_messages.clear()


@dataclass
class _ParseContext:
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_id: str | None = None
    thinking_parts: list[ThoughtPart] = field(default_factory=list)
    result_parts: list[str] = field(default_factory=list)
    content_parts: list[str] = field(default_factory=list)
    content_delta_parts: list[str] = field(default_factory=list)
    reasoning_delta_parts: list[str] = field(default_factory=list)
    reasoning_final_parts: list[str] = field(default_factory=list)
    error_parts: list[str] = field(default_factory=list)
    saw_error: bool = False


def _iter_events(stdout: str):
    """按行解析 stdout 为 JSON 事件流。非 JSON 行作为原文正文产出。"""
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            yield {"__plain_text__": line}
            continue
        if isinstance(event, dict):
            yield event


def _event_type(event: dict[str, Any]) -> str:
    return str(event.get("type") or event.get("event") or event.get("kind") or "").lower()


def _dispatch_process_event(ctx: _ParseContext, event: dict[str, Any], event_type: str) -> bool:
    """如果是工具调用/推理过程事件，提取为 ThoughtPart 并返回 True。"""
    parts = _format_process_event(event, event_type)
    if parts:
        ctx.thinking_parts.extend(parts)
        return True
    return False


def _dispatch_result(ctx: _ParseContext, event: dict[str, Any], event_type: str) -> bool:
    """处理 result / final / completion 类型事件，提取权威最终回复。"""
    if event_type != "result" and "final" not in event_type and "completion" not in event_type:
        return False
    text = event.get("result") if event_type == "result" else None
    if not isinstance(text, str) or not text.strip():
        text = _extract_text(event)
    if text and text.strip():
        ctx.result_parts.append(_strip_inline_thinking(ctx, text.strip()))
    return True


def _split_assistant_content(event: dict[str, Any]) -> tuple[list[ThoughtPart], str]:
    """从 assistant 事件的 message.content[] 中分离过程流（思考/工具）与正文（text/markdown）。

    thinking/reasoning/tool_use/tool_result 等过程块以 ThoughtPart 形式返回；text/markdown
    块原样拼接到正文流。
    """
    content = _event_content_blocks(event)
    if not isinstance(content, list):
        return [], _extract_text(content)
    process_parts: list[ThoughtPart] = []
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            text = _extract_text(block)
            if text:
                text_parts.append(text)
            continue
        block_type = str(block.get("type") or block.get("tag") or "").lower()
        if block_type in {"thinking", "reasoning"}:
            text = _extract_text(block.get("thinking") or block.get("text") or block.get("content") or block)
            if text:
                process_parts.append(ThoughtPart("thinking", text=text))
        elif block_type in {"tool_use", "tool_call", "server_tool_use"}:
            process_parts.append(_format_tool_use(block))
        elif block_type in {"tool_result", "tool_response"}:
            text = _extract_text(block.get("content") or block.get("result") or block)
            if text:
                process_parts.append(ThoughtPart("tool_result", text=text))
        elif block_type in {"text", "markdown"}:
            text = _extract_text(block.get("text") or block.get("content") or block)
            if text:
                text_parts.append(text)
    return process_parts, "\n".join(text_parts)


_THINKING_INLINE_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL)


def _strip_inline_thinking(ctx: _ParseContext, text: str) -> str:
    """把模型直接写在正文 text 里的 <think>/<thinking> 抽成 thinking 流，返回干净正文。

    主要用于 fuyao-mimo / Claude 等把 reasoning 内联在 text content 里的模型；普通 text 不受影响。
    """
    if "<think" not in text:
        return text
    clean_parts: list[str] = []
    last = 0
    for match in _THINKING_INLINE_RE.finditer(text):
        clean_parts.append(text[last:match.start()])
        thinking = match.group(1).strip()
        if thinking:
            ctx.thinking_parts.append(ThoughtPart("thinking", text=thinking))
        last = match.end()
    clean_parts.append(text[last:])
    return "".join(clean_parts).strip()


def _fallback_result(parts: list[Any]) -> str:
    for part in reversed(parts):
        text = part.text if isinstance(part, ThoughtPart) else str(part)
        cleaned = text.strip()
        if cleaned:
            return cleaned
    return ""


def _pi_toolcall(inner: dict[str, Any]) -> tuple[str, ThoughtPart]:
    """Extract (callId, tool_use part) from a pi toolcall_end event.

    toolcall_end carries the complete toolCall block (id + name + full arguments);
    the id matches the toolCallId on tool_execution_* events, enabling pairing.
    """
    partial = inner.get("partial")
    if not isinstance(partial, dict):
        return "", ""
    content = partial.get("content")
    if not isinstance(content, list):
        return "", ""
    idx = inner.get("contentIndex")
    block = None
    if isinstance(idx, int) and 0 <= idx < len(content):
        b = content[idx]
        if isinstance(b, dict) and str(b.get("type") or "") == "toolCall":
            block = b
    if block is None:
        for b in content:
            if isinstance(b, dict) and str(b.get("type") or "") == "toolCall":
                block = b
                break
    if block is None:
        return "", ""
    call_id = str(block.get("id") or "")
    name = str(block.get("name") or "tool")
    args = block.get("arguments") or block.get("partialArgs")
    return call_id, _format_tool_use({"name": name, "input": args})


def _format_pi_tool_execution(event: dict[str, Any]) -> ThoughtPart:
    """Extract a tool_use part from a pi tool_execution_start event."""
    name = str(event.get("toolName") or "tool")
    return _format_tool_use({"name": name, "input": event.get("args")})


def _format_pi_tool_result(event: dict[str, Any]) -> ThoughtPart | None:
    """Extract a tool_result part from a pi tool_execution_end event (full content)."""
    text = _extract_text(event.get("toolResult") or event.get("result") or "")
    if not text:
        return None
    return ThoughtPart(kind="tool_result", text=text, is_error=bool(event.get("isError")))


def _format_pi_tool_partial(event: dict[str, Any]) -> ThoughtPart | None:
    """Extract a tool_partial part from a pi tool_execution_update event."""
    text = _extract_text(event.get("partialResult"))
    if not text:
        return None
    return ThoughtPart(kind="tool_partial", text=text)


def _format_copilot_tool_request(data: dict[str, Any]) -> ThoughtPart:
    """Extract a tool_use part from copilot tool request data."""
    name = str(data.get("toolName") or data.get("name") or "tool")
    args = data.get("arguments") or data.get("inputDelta") or data.get("args")
    return _format_tool_use({"name": name, "input": args})


def _format_copilot_tool_execution(event: dict[str, Any]) -> ThoughtPart | None:
    """Extract a tool result/partial part from copilot tool.execution_* events.

    tool.execution_start is skipped (its toolName+arguments duplicate
    assistant.message.toolRequests[]); execution_complete carries the outcome.
    """
    event_type = str(event.get("type") or "")
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    if event_type.endswith("execution_start"):
        return None
    if event_type.endswith("execution_complete"):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return ThoughtPart(kind="tool_result", text=str(err["message"]), is_error=True)
        if data.get("success") is False:
            return ThoughtPart(kind="tool_result", text="failed", is_error=True)
        text = _extract_text(data.get("result"))
        if text:
            return ThoughtPart(kind="tool_result", text=text)
        return None
    if event_type.endswith("partial_result"):
        text = _extract_text(data.get("result") or data.get("partialResult"))
        if text:
            return ThoughtPart(kind="tool_partial", text=text)
    return None


def _format_process_event(event: dict[str, Any], event_type: str) -> list[ThoughtPart]:
    parts: list[ThoughtPart] = []
    content = _event_content_blocks(event)
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or block.get("tag") or block.get("kind") or "").lower()
            if block_type in {"thinking", "reasoning", "reasoning_delta"}:
                text = _extract_text(block.get("thinking") or block.get("text") or block.get("content") or block)
                if text:
                    parts.append(ThoughtPart("thinking", text=text))
            elif block_type in {"tool_use", "tool_call", "server_tool_use"}:
                parts.append(_format_tool_use(block))
            elif block_type in {"tool_result", "tool_response"}:
                text = _extract_text(block.get("content") or block.get("result") or block)
                if text:
                    parts.append(ThoughtPart("tool_result", text=text))
    if not parts and "tool" in event_type:
        if "result" in event_type or "response" in event_type:
            text = _extract_text(event.get("data") or event.get("result") or event)
            if text:
                parts.append(ThoughtPart("tool_result", text=text))
        else:
            parts.append(_format_tool_use(event.get("data") if isinstance(event.get("data"), dict) else event))
    return parts


def _event_content_blocks(event: dict[str, Any]) -> Any:
    message = event.get("message")
    if isinstance(message, dict) and "content" in message:
        return message.get("content")
    data = event.get("data")
    if isinstance(data, dict) and "content" in data:
        return data.get("content")
    return event.get("content")


def _format_tool_use(block: dict[str, Any]) -> ThoughtPart:
    """Extract a tool_use part (name + raw args). Emoji/compact/skill rendering lives in format_thinking."""
    name = str(block.get("name") or block.get("tool_name") or block.get("tool") or block.get("function") or "tool")
    args = block.get("input") or block.get("arguments") or block.get("args") or block.get("parameters")
    return ThoughtPart(kind="tool_use", name=name, args=args)


def _compact_json(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


def format_thinking(parts: list[ThoughtPart], config: Any = None) -> str:
    """中间格式化层：把 parser 产出的结构化 parts 渲染为展示字符串。

    所有展示变换集中在此——加 emoji、截断 tool result。parser 只产 parts，不负责格式。
    config 为 None 时不截断（供测试/调试）。
    """
    limit = _tool_result_limit(config)
    out: list[str] = []
    for part in parts:
        rendered = _render_part(part, limit)
        if rendered:
            out.append(rendered)
    return "\n".join(out).strip()


def _render_part(part: ThoughtPart, limit: int) -> str:
    if part.kind == "thinking" and part.text:
        return f"🧠 Thinking\n{part.text}"
    if part.kind == "tool_use":
        return _render_tool_use(part)
    if part.kind == "tool_result":
        text = _truncate(part.text, limit)
        if not text:
            return ""
        label = "📎 Tool result (error)" if part.is_error else "📎 Tool result"
        return f"{label}\n{text}"
    if part.kind == "tool_partial":
        text = _truncate(part.text, limit)
        if not text:
            return ""
        return f"📎 Tool partial\n{text}"
    if part.kind == "command" and part.text:
        return f"🛠 Command\n{part.text}"
    if part.kind == "command_result":
        text = _truncate(part.text, limit)
        if not text:
            return ""
        label = "📎 Command result (error)" if part.is_error else "📎 Command result"
        return f"{label}\n{text}"
    if part.kind == "warning" and part.text:
        return f"⚠️ Error\n{part.text}"
    if part.kind == "agent" and part.text:
        return f"💬 Agent\n{part.text}"
    if part.kind == "text" and part.text:
        return part.text
    return ""


def _render_tool_use(part: ThoughtPart) -> str:
    name = part.name or "tool"
    if name.lower() in {"skill", "skills"}:
        skill_name = ""
        if isinstance(part.args, dict):
            skill_name = str(part.args.get("skill") or part.args.get("name") or "")
        return f"📚 Skill {skill_name}".strip()
    detail = _compact_json(part.args)
    return f"🛠 Tool {name}" + (f"\n{detail}" if detail else "")


def _tool_result_limit(config: Any) -> int:
    get = getattr(config, "get", None)
    if not callable(get):
        return 0
    try:
        return int(get("options.features.show_max_tool_result_length", 0))
    except (TypeError, ValueError):
        return 0


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


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
    candidate_keys = (
        "input_tokens", "prompt_tokens", "inputTokens", "input",
        "output_tokens", "completion_tokens", "outputTokens", "output",
        "cached_tokens", "cache_read_input_tokens", "cached_input_tokens", "cache_creation_input_tokens",
        "cacheWriteInputTokens", "cache_write_input_tokens",
        "cacheReadInputTokens", "cacheCreationInputTokens",
        "cacheRead", "cacheWrite",
        "reasoning", "reasoning_output_tokens",
    )
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if any(key in current for key in candidate_keys):
                candidates.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)

    def take(keys: tuple[str, ...]) -> int | None:
        best: int | None = None
        for item in candidates:
            v = _first_int(item, keys, None)
            if v is None:
                continue
            best = v if best is None else max(best, v)
        return best

    input_t = take(("input_tokens", "prompt_tokens", "inputTokens", "input"))
    output_t = take(("output_tokens", "completion_tokens", "outputTokens", "output"))
    reasoning_t = take(("reasoning", "reasoning_output_tokens"))
    cache_read = take((
        "cached_tokens", "cache_read_input_tokens", "cached_input_tokens", "cacheReadInputTokens", "cacheRead",
    ))
    cache_write = take((
        "cache_creation_input_tokens", "cacheWriteInputTokens", "cache_write_input_tokens", "cacheWrite",
    ))
    # When read and creation are both present in the same usage object they refer to
    # disjoint portions of the same cache budget (not additive). Take the larger so the
    # card reports a single representative cache number without double-counting.
    cache_t: int | None = None
    for v in (cache_read, cache_write):
        if v is None:
            continue
        cache_t = v if cache_t is None else max(cache_t, v)

    if input_t is not None:
        usage.input_tokens = input_t
    if cache_t is not None:
        usage.cached_tokens = cache_t
    if output_t is not None:
        # Reasoning tokens are generated output; add to output total
        total_out = output_t
        if reasoning_t:
            total_out += reasoning_t
        usage.output_tokens = total_out
    return usage


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


def _accumulate_pi_usage(target: TokenUsage, source: Any) -> None:
    """Accumulate Pi per-turn usage into target.

    Pi reports per-turn usage (not cumulative), so we sum input/output across turns.
    Cache is cumulative across turns — take the latest (max) value.
    """
    if not isinstance(source, dict):
        return
    input_t = _first_int(source, ("input", "input_tokens"), None)
    output_t = _first_int(source, ("output", "output_tokens"), None)
    reasoning_t = _first_int(source, ("reasoning",), None)
    cache_read = _first_int(source, ("cacheRead", "cache_read_input_tokens", "cached_tokens"), None)
    cache_write = _first_int(source, ("cacheWrite", "cache_creation_input_tokens"), None)
    if input_t is not None:
        target.input_tokens = (target.input_tokens or 0) + input_t
    if output_t is not None:
        total_out = output_t + (reasoning_t or 0)
        target.output_tokens = (target.output_tokens or 0) + total_out
    cache_t: int | None = None
    for v in (cache_read, cache_write):
        if v is None:
            continue
        cache_t = v if cache_t is None else max(cache_t, v)
    if cache_t is not None:
        target.cached_tokens = max(target.cached_tokens or 0, cache_t)


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
