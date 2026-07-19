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


@registry.exact(["/help"], "显示帮助信息")
def _help(ctx: CommandContext) -> CommandResponse:
    return CommandResponse(cards.build_help_card(registry.help_items()))


@registry.prefix("/cli", "切换或查看 AI CLI，例如 /cli qoder")
def _cli(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) == 1:
        tool = ctx.config.current_tool()
        model = ctx.config.resolve_model(tool)
        lines = [f"当前：`{tool}` / `{model}`", "", "**支持的 CLI**"]
        for name in ctx.config.tools():
            lines.append(f"- `{name}`：默认模型 `{ctx.config.default_model(name)}`")
        return CommandResponse(cards.build_generic_card("当前 AI CLI", "\n".join(lines), "blue"))
    tool = parts[1].strip().lower()
    try:
        model = ctx.config.set_tool(tool)
    except ConfigError as exc:
        return CommandResponse(cards.build_error_card("CLI 切换失败", str(exc)))
    return CommandResponse(cards.build_generic_card("AI CLI 已切换", f"当前工具：`{tool}`\n当前模型：`{model}`", "green"))


@registry.prefix("/model", "切换或查看模型，例如 /model max")
def _model(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    tool = ctx.config.current_tool()
    if len(parts) == 1:
        options = ctx.config.model_options(tool)
        lines = [
            f"当前 CLI：`{tool}`",
            f"默认模型：`{options['default_model']}`",
            f"当前模型：`{options['current_model']}`",
            "",
            "**支持模型**",
        ]
        lines.extend(f"- `{model}`" for model in options["model_list"])
        if options["aliases"]:
            lines.extend(["", "**别名**"])
            lines.extend(f"- `{alias}` → `{model}`" for alias, model in options["aliases"].items())
        return CommandResponse(cards.build_generic_card("模型配置", "\n".join(lines), "blue"))
    try:
        model = ctx.config.set_model(tool, parts[1].strip())
    except ConfigError as exc:
        return CommandResponse(cards.build_error_card("模型切换失败", str(exc)))
    return CommandResponse(cards.build_generic_card("模型已切换", f"`{tool}` → `{model}`", "green"))


@registry.exact(["/reload"], "重新加载 config.json 和 model.json")
def _reload(ctx: CommandContext) -> CommandResponse:
    try:
        ctx.config.reload()
        cards.configure_card_width(str(ctx.config.get("feishu.card_width", "half")))
        cards.configure_tool_icons(ctx.config.get("feishu.tool_icons", {}))
    except Exception as exc:
        return CommandResponse(cards.build_error_card("配置重载失败", str(exc)))
    return CommandResponse(cards.build_generic_card("配置已重新加载", "已从 `config.json` 和 `model.json` 读取最新配置。", "green"))


@registry.exact(["/status"], "展示版本、CLI、模型、运行任务和定时任务")
def _status(ctx: CommandContext) -> CommandResponse:
    tool = ctx.config.current_tool()
    model = ctx.config.resolve_model(tool)
    running = ctx.ai_runner.running()
    timers = ctx.scheduler.list_tasks() if ctx.scheduler else []
    content = "\n".join(
        [
            f"- 版本：`{ctx.config.version()}`",
            f"- AI CLI：`{tool}`",
            f"- 模型：`{model}`",
            f"- 正在运行：`{len(running)}`",
            f"- 定时任务：`{len(timers)}`",
        ]
    )
    return CommandResponse(cards.build_generic_card("FeishuBot 状态", content, "blue"))


@registry.exact(["/timer"], "列出所有定时任务")
def _timer(ctx: CommandContext) -> CommandResponse:
    items = ctx.scheduler.list_tasks() if ctx.scheduler else []
    if not items:
        return CommandResponse(cards.build_generic_card("定时任务", "当前没有定时任务。", "blue"))
    return CommandResponse(cards.build_timer_list_card(items))


@registry.prefix("/timer-rm", "删除指定定时任务，例如 /timer-rm task-id")
def _timer_rm(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    if len(parts) == 1:
        return CommandResponse(cards.build_error_card("删除失败", "请提供任务 ID。"))
    removed = ctx.scheduler.delete_task(parts[1].strip()) if ctx.scheduler else False
    if not removed:
        return CommandResponse(cards.build_error_card("删除失败", "没有找到指定任务。"))
    return CommandResponse(cards.build_generic_card("定时任务已删除", f"`{parts[1].strip()}`", "green"))


@registry.exact(["/running"], "查看当前运行中的 AI 分析任务")
def _running(ctx: CommandContext) -> CommandResponse:
    items = ctx.ai_runner.running()
    if not items:
        return CommandResponse(cards.build_generic_card("运行中的 AI 分析", "当前没有运行中的任务。", "blue"))
    return CommandResponse(cards.build_running_tasks_card(items))


@registry.prefix("/kill", "终止 AI 分析进程，例如 /kill <id>，回复消息时可直接 /kill")
def _kill(ctx: CommandContext) -> CommandResponse:
    parts = ctx.text.split(maxsplit=1)
    target_id = parts[1].strip() if len(parts) > 1 else ctx.message.get("parent_id")
    if not target_id:
        return CommandResponse(cards.build_error_card("终止失败", "请提供 `/running` 中显示的任务 ID；回复正在分析的消息时也可以直接发送 `/kill`。"))
    killed_id = ctx.ai_runner.kill(target_id)
    if not killed_id:
        return CommandResponse(cards.build_error_card("终止失败", f"没有找到运行中的 AI 进程：`{target_id}`。"))
    return CommandResponse(cards.build_error_card("已终止 AI 分析", f"已向任务 `{killed_id}` 发送终止信号。"))
