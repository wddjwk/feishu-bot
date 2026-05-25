from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import cards
from config import Config, ConfigError


@dataclass
class CommandContext:
    text: str
    message: dict
    config: Config
    session_store: object
    ai_runner: object
    scheduler: object
    messenger: object


@dataclass
class CommandResponse:
    card: dict


CommandHandler = Callable[[CommandContext], CommandResponse]


class CommandRegistry:
    def __init__(self) -> None:
        self._exact: dict[str, CommandHandler] = {}
        self._prefix: dict[str, CommandHandler] = {}
        self._help: list[tuple[str, str]] = []

    def exact(self, names: list[str], help_text: str) -> Callable[[CommandHandler], CommandHandler]:
        def decorator(fn: CommandHandler) -> CommandHandler:
            for name in names:
                self._exact[name] = fn
            self._help.append((names[0], help_text))
            return fn

        return decorator

    def prefix(self, name: str, help_text: str) -> Callable[[CommandHandler], CommandHandler]:
        def decorator(fn: CommandHandler) -> CommandHandler:
            self._prefix[name] = fn
            self._help.append((name, help_text))
            return fn

        return decorator

    def match(self, ctx: CommandContext) -> CommandResponse | None:
        text = ctx.text.strip()
        if not text:
            return None
        exact = self._exact.get(text)
        if exact:
            return exact(ctx)
        for prefix, handler in self._prefix.items():
            if text == prefix or text.startswith(prefix + " "):
                return handler(ctx)
        return None

    def help_items(self) -> list[tuple[str, str]]:
        return list(self._help)


registry = CommandRegistry()


@registry.exact(["/help", "帮助", "?"], "显示帮助卡片")
def _help(ctx: CommandContext) -> CommandResponse:
    return CommandResponse(cards.build_help_card(registry.help_items()))


@registry.prefix("/cli", "切换或查看 AI CLI，例如 /cli qoder")
def _cli(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) == 1:
        tool = ctx.config.current_tool()
        return CommandResponse(cards.build_generic_card("当前 AI CLI", f"`{tool}`", "blue"))
    tool = parts[1].strip().lower()
    try:
        ctx.config.set_tool(tool)
    except ConfigError as exc:
        supported = ", ".join(ctx.config.tools())
        return CommandResponse(cards.build_error_card("CLI 切换失败", str(exc), f"支持：{supported}"))
    model = ctx.config.resolve_model(tool)
    return CommandResponse(cards.build_generic_card("AI CLI 已切换", f"当前工具：`{tool}`\n当前模型：`{model}`", "green"))


@registry.prefix("/model", "切换或查看模型，例如 /model max")
def _model(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    tool = ctx.config.current_tool()
    if len(parts) == 1:
        lines = [f"- `{name}`：`{model}`" for name, model in ctx.config.model_status().items()]
        return CommandResponse(cards.build_generic_card("当前模型配置", "\n".join(lines), "blue"))
    try:
        model = ctx.config.set_model(tool, parts[1].strip())
    except ConfigError as exc:
        return CommandResponse(cards.build_error_card("模型切换失败", str(exc)))
    return CommandResponse(cards.build_generic_card("模型已切换", f"`{tool}` → `{model}`", "green"))


@registry.exact(["/reload"], "重新加载 config.json")
def _reload(ctx: CommandContext) -> CommandResponse:
    try:
        ctx.config.reload()
    except Exception as exc:
        return CommandResponse(cards.build_error_card("配置重载失败", str(exc)))
    return CommandResponse(cards.build_generic_card("配置已重新加载", "已从 `config.json` 读取最新配置。", "green"))


@registry.exact(["/status"], "展示版本、工具、模型、超时与调度端口")
def _status(ctx: CommandContext) -> CommandResponse:
    tool = ctx.config.current_tool()
    model = ctx.config.resolve_model(tool)
    content = "\n".join(
        [
            f"- 版本：`{ctx.config.version()}`",
            f"- AI CLI：`{tool}`",
            f"- 模型：`{model}`",
            f"- 超时：`{ctx.config.get('ai.timeout_seconds')}s`",
            f"- 调度端口：`{ctx.config.get('scheduler.port')}`",
        ]
    )
    return CommandResponse(cards.build_generic_card("FeishuBot 状态", content, "blue"))


@registry.exact(["/timer"], "列出所有定时任务")
def _timer(ctx: CommandContext) -> CommandResponse:
    items = ctx.scheduler.list_tasks() if ctx.scheduler else []
    if not items:
        return CommandResponse(cards.build_generic_card("定时任务", "当前没有定时任务。", "blue"))
    lines = []
    for item in items:
        status = "启用" if item.get("enabled", True) else "停用"
        lines.append(f"- `{item['id']}` `{item['cron']}` {item['type']} {status}：{item.get('name', '')}")
    return CommandResponse(cards.build_generic_card("定时任务", "\n".join(lines), "blue"))


@registry.prefix("/timer-rm", "删除指定定时任务，例如 /timer-rm task-id")
def _timer_rm(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) == 1:
        return CommandResponse(cards.build_error_card("删除失败", "请提供任务 ID。"))
    removed = ctx.scheduler.delete_task(parts[1].strip()) if ctx.scheduler else False
    if not removed:
        return CommandResponse(cards.build_error_card("删除失败", "没有找到指定任务。"))
    return CommandResponse(cards.build_generic_card("定时任务已删除", f"`{parts[1].strip()}`", "green"))


@registry.exact(["/kill"], "终止被回复消息关联的 AI 分析进程")
def _kill(ctx: CommandContext) -> CommandResponse:
    target_id = ctx.message.get("parent_id")
    if not target_id:
        return CommandResponse(cards.build_error_card("终止失败", "`/kill` 需要回复某条正在分析的消息。"))
    killed = ctx.ai_runner.kill(target_id)
    if not killed:
        return CommandResponse(cards.build_error_card("终止失败", "没有找到该消息关联的运行中 AI 进程。"))
    return CommandResponse(cards.build_generic_card("已终止 AI 分析", f"消息 `{target_id}` 的分析进程已被终止。", "green"))
