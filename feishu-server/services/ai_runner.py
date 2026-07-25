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
            ctx.thinking_parts.extend(ctx.reasoning_delta_parts)
        elif ctx.reasoning_final_parts:
            ctx.thinking_parts.extend(ctx.reasoning_final_parts)
        _merge_usage(ctx.usage, _usage_from_stderr(stderr))
        if ctx.content_delta_parts and not ctx.result_parts:
            ctx.result_parts.append("".join(ctx.content_delta_parts).strip())
        result = "\n".join(part for part in ctx.result_parts if part).strip()
        if not result:
            result = _fallback_result(ctx.content_parts or ctx.thinking_parts)
        thinking = "\n".join(part for part in ctx.thinking_parts if part).strip()
        return AIResult(
            ok=bool(result) and not ctx.saw_error,
            tool=self.name,
            model="",
            thinking=thinking,
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
            thinking_text, body_text = _split_assistant_content(event)
            if thinking_text:
                ctx.thinking_parts.append(thinking_text)
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

    内容提取策略：
      - thinking_delta / text_delta：收集到 content_delta_parts 作为兜底。正常情况下
        turn_end 的完整 content 块是权威来源，deltas 仅在 turn_end 缺失时使用。
      - toolcall_end：渲染为 🛠 Tool 行。中间轮工具调用记录，turn_end 的 content
        中虽有 toolCall 块但与之重复，故不从 turn_end 提取 toolCall。
      - tool_execution_start / tool_execution_update / tool_execution_end：
        顶层工具执行事件，与 toolcall_end 是不同粒度（调用 vs 执行细节），全部渲染。
      - turn_end：核心事件。从 ALL turn_end 的 content 提取 thinking 块（每轮的推理）；
        从最终 turn（stopReason=="stop"）提取 text 块作为正文。usage 逐轮累加。
      - session：提取 session id。
      - agent_end / agent_settled / message_start / message_end / turn_start：结构事件。
    """

    name = "pi-json"

    def _handle_event(self, ctx: "_ParseContext", event: dict[str, Any], event_type: str) -> None:
        if event_type == "session":
            sid = event.get("id")
            if isinstance(sid, str) and sid:
                ctx.session_id = ctx.session_id or sid
            return
        if event_type == "message_update":
            inner = event.get("assistantMessageEvent")
            if not isinstance(inner, dict):
                return
            inner_type = str(inner.get("type") or "").lower()
            if inner_type == "toolcall_end":
                text = _format_pi_toolcall_end(inner)
                if text:
                    ctx.thinking_parts.append(text)
            elif inner_type == "text_delta":
                delta = inner.get("delta")
                if isinstance(delta, str) and delta:
                    ctx.content_delta_parts.append(_strip_inline_thinking(ctx, delta))
            # thinking_start/delta, text_start, toolcall_start/delta: skipped
            # (turn_end carries the complete content blocks — see class docstring)
            return
        if event_type == "tool_execution_start":
            text = _format_pi_tool_execution(event)
            if text:
                ctx.thinking_parts.append(text)
            return
        if event_type == "tool_execution_end":
            text = _format_pi_tool_result(event)
            if text:
                ctx.thinking_parts.append(text)
            return
        if event_type == "tool_execution_update":
            text = _format_pi_tool_partial(event)
            if text:
                ctx.thinking_parts.append(text)
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
                                ctx.thinking_parts.append(f"🧠 Thinking\n{text}")
                        elif block_type == "text":
                            text = block.get("text") or ""
                            if text:
                                clean = _strip_inline_thinking(ctx, text)
                                if clean:
                                    if is_final:
                                        ctx.result_parts.append(clean)
                                    else:
                                        ctx.thinking_parts.append(clean)
                        # toolCall blocks skipped — duplicate toolcall_end events
            return
        # agent_end / agent_settled / message_start / message_end / turn_start: no-op


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


@dataclass
class _ParseContext:
    usage: TokenUsage = field(default_factory=TokenUsage)
    session_id: str | None = None
    thinking_parts: list[str] = field(default_factory=list)
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
    """如果是工具调用/推理过程事件，格式化到思考流并返回 True。"""
    process_text = _format_process_event(event, event_type)
    if process_text:
        ctx.thinking_parts.append(process_text)
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


def _split_assistant_content(event: dict[str, Any]) -> tuple[str, str]:
    """从 assistant 事件的 message.content[] 中分离过程流（思考/工具）与正文（text/markdown）。

    thinking/reasoning/tool_use 等过程块使用与 _format_process_event 一致的 emoji 前缀格式，
    以保证卡片中"分析过程"区域的展示效果统一；text/markdown 块原样拼接到正文流。
    """
    content = _event_content_blocks(event)
    if not isinstance(content, list):
        return "", _extract_text(content)
    process_parts: list[str] = []
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
                process_parts.append(f"🧠 Thinking\n{text}")
        elif block_type in {"tool_use", "tool_call", "server_tool_use"}:
            process_parts.append(_format_tool_use(block))
        elif block_type in {"tool_result", "tool_response"}:
            text = _extract_text(block.get("content") or block.get("result") or block)
            if text:
                process_parts.append(f"📎 Tool result\n{text}")
        elif block_type in {"text", "markdown"}:
            text = _extract_text(block.get("text") or block.get("content") or block)
            if text:
                text_parts.append(text)
    return "\n\n".join(process_parts), "\n".join(text_parts)


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
            ctx.thinking_parts.append(f"🧠 Thinking\n{thinking}")
        last = match.end()
    clean_parts.append(text[last:])
    return "".join(clean_parts).strip()


def _fallback_result(parts: list[str]) -> str:
    for part in reversed(parts):
        cleaned = part.strip()
        if cleaned:
            return cleaned
    return ""


def _format_pi_toolcall_end(inner: dict[str, Any]) -> str:
    """Format pi's message_update.inner event of type toolcall_end.

    toolcall_end carries the complete toolCall block (name + full arguments).
    toolcall_start / toolcall_delta carry partial state and are not rendered.
    """
    partial = inner.get("partial")
    if not isinstance(partial, dict):
        return ""
    content = partial.get("content")
    if not isinstance(content, list):
        return ""
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
        return ""
    name = str(block.get("name") or "tool")
    args = block.get("arguments") or block.get("partialArgs")
    return _format_tool_use({"name": name, "input": args})


def _format_pi_tool_execution(event: dict[str, Any]) -> str:
    """Format a pi tool_execution_start event as a tool-call line."""
    name = str(event.get("toolName") or "tool")
    return _format_tool_use({"name": name, "input": event.get("args")})


def _format_pi_tool_result(event: dict[str, Any]) -> str:
    """Format a pi tool_execution_end event as a tool-result line. Full content, no truncation."""
    label = "📎 Tool result (error)" if event.get("isError") else "📎 Tool result"
    text = _extract_text(event.get("toolResult") or event.get("result") or "")
    return f"{label}\n{text}" if text else ""


def _format_pi_tool_partial(event: dict[str, Any]) -> str:
    """Format a pi tool_execution_update event (streaming partialResult). Full content."""
    partial = event.get("partialResult")
    text = _extract_text(partial)
    if not text:
        return ""
    return f"📎 Tool partial\n{text}"


def _format_copilot_tool_request(data: dict[str, Any]) -> str:
    """Format copilot tool request (from assistant.tool_call_delta or assistant.message.toolRequests[])."""
    name = str(data.get("toolName") or data.get("name") or "tool")
    args = data.get("arguments") or data.get("inputDelta") or data.get("args")
    return _format_tool_use({"name": name, "input": args})


def _format_copilot_tool_execution(event: dict[str, Any]) -> str:
    """Format copilot tool.execution_complete/partial_result events.

    tool.execution_start is handled upstream (skipped here — its toolName+arguments
    duplicate assistant.message.toolRequests[]; we prefer the complete form).
    execution_complete carries the execution outcome (success/error/result), which
    is unique to execution and not duplicated anywhere else.
    """
    event_type = str(event.get("type") or "")
    data = event.get("data")
    if not isinstance(data, dict):
        return ""
    if event_type.endswith("execution_start"):
        # Duplicate of assistant.message.toolRequests[] — skip
        return ""
    if event_type.endswith("execution_complete"):
        err = data.get("error")
        if isinstance(err, dict) and err.get("message"):
            return f"📎 Tool result (error)\n{err['message']}"
        if data.get("success") is False:
            return "📎 Tool result (error)\nfailed"
        # success=true: render the result if present; otherwise absence of failure has no payload
        result = data.get("result")
        text = _extract_text(result)
        if text:
            return f"📎 Tool result\n{text}"
        return ""
    if event_type.endswith("partial_result"):
        result = data.get("result") or data.get("partialResult")
        text = _extract_text(result)
        if text:
            return f"📎 Tool partial\n{text}"
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
                    parts.append(f"📎 Tool result\n{text}")
    if not parts and "tool" in event_type:
        if "result" in event_type or "response" in event_type:
            text = _extract_text(event.get("data") or event.get("result") or event)
            if text:
                parts.append(f"📎 Tool result\n{text}")
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


def _compact_json(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


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
        "cached_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
        "cacheReadInputTokens", "cacheCreationInputTokens",
        "cacheRead", "cacheWrite",
        "reasoning",
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
    reasoning_t = take(("reasoning",))
    cache_read = take((
        "cached_tokens", "cache_read_input_tokens", "cacheReadInputTokens", "cacheRead",
    ))
    cache_write = take((
        "cache_creation_input_tokens", "cacheCreationInputTokens", "cacheWrite",
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
