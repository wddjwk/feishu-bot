from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import logger


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_MODEL_PATH = BASE_DIR / "model.json"


class ConfigError(RuntimeError):
    pass


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"{label} 不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} 不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} 根节点必须是 JSON 对象")
    return data


class Config:
    def __init__(
        self,
        path: Path | str = DEFAULT_CONFIG_PATH,
        model_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.model_path = Path(model_path) if model_path else self.path.with_name("model.json")
        self._data: dict[str, Any] = {}
        self._models: dict[str, Any] = {}
        self._runtime_tool: str | None = None
        self._runtime_models: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        server_data = _read_json(self.path, "config.json")
        model_data = _read_json(self.model_path, "model.json")
        self._validate(server_data, model_data)
        self._data = copy.deepcopy(server_data)
        self._models = copy.deepcopy(model_data)
        if self._runtime_tool not in self.tools():
            self._runtime_tool = None
        self._runtime_models = {
            tool: model
            for tool, model in self._runtime_models.items()
            if tool in self.tools()
        }
        logger.info(
            "配置已加载：工具=%s 默认CLI=%s",
            ",".join(self.tools()),
            self._models.get("default_cli"),
        )

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def model_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._models)

    def get(self, dotted: str, default: Any = None) -> Any:
        return self._get(self._data, dotted, default)

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        current = self._data
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    def base_dir(self) -> Path:
        return self.path.parent

    def resolve_path(self, dotted: str) -> Path:
        value = self.get(dotted)
        if not value:
            raise ConfigError(f"缺少路径配置：{dotted}")
        path = Path(str(value))
        if not path.is_absolute():
            path = self.base_dir() / path
        return path.resolve()

    def tools(self) -> list[str]:
        server_tools = self.get("ai.tools", {})
        model_tools = self._models.get("tools", {})
        if not isinstance(server_tools, dict) or not isinstance(model_tools, dict):
            return []
        return sorted(set(server_tools) & set(model_tools))

    def default_tool(self) -> str:
        tool = str(self._models.get("default_cli") or "").strip()
        if tool not in self.tools():
            raise ConfigError(f"model.json 的 default_cli 无效：{tool}")
        return tool

    def current_tool(self) -> str:
        return self._runtime_tool or self.default_tool()

    def set_tool(self, tool: str) -> str:
        if tool not in self.tools():
            raise ConfigError(f"不支持的 AI CLI：{tool}")
        self._runtime_tool = tool
        model = self.default_model(tool)
        self._runtime_models[tool] = model
        return model

    def tool_config(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        cfg = self._get(self._data, f"ai.tools.{selected}")
        if not isinstance(cfg, dict):
            raise ConfigError(f"缺少 AI CLI 启动配置：{selected}")
        return copy.deepcopy(cfg)

    def model_config(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        tools = self._models.get("tools", {})
        cfg = tools.get(selected) if isinstance(tools, dict) else None
        if not isinstance(cfg, dict):
            raise ConfigError(f"缺少 AI CLI 模型配置：{selected}")
        return copy.deepcopy(cfg)

    def default_model(self, tool: str | None = None) -> str:
        selected = tool or self.current_tool()
        model = self.model_config(selected).get("default_model")
        if not isinstance(model, str) or not model.strip():
            raise ConfigError(f"AI CLI {selected} 缺少 default_model")
        return model.strip()

    def resolve_model(self, tool: str | None = None, requested: str | None = None) -> str:
        selected = tool or self.current_tool()
        if requested is not None:
            name = requested.strip()
            if not name:
                raise ConfigError("模型名称不能为空")
            aliases = self.model_config(selected).get("aliases", {})
            return str(aliases.get(name, name)) if isinstance(aliases, dict) else name
        return self._runtime_models.get(selected, self.default_model(selected))

    def set_model(self, tool: str | None, requested: str) -> str:
        selected = tool or self.current_tool()
        if selected not in self.tools():
            raise ConfigError(f"不支持的 AI CLI：{selected}")
        model = self.resolve_model(selected, requested)
        self._runtime_models[selected] = model
        return model

    def model_options(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        cfg = self.model_config(selected)
        model_list = cfg.get("model_list", [])
        aliases = cfg.get("aliases", {})
        if not isinstance(model_list, list) or not all(isinstance(model, str) for model in model_list):
            raise ConfigError(f"AI CLI {selected} 的 model_list 必须是字符串数组")
        if not isinstance(aliases, dict) or not all(isinstance(name, str) and isinstance(model, str) for name, model in aliases.items()):
            raise ConfigError(f"AI CLI {selected} 的 aliases 必须是字符串映射")
        return {
            "default_model": self.default_model(selected),
            "current_model": self.resolve_model(selected),
            "model_list": list(model_list),
            "aliases": dict(aliases),
        }

    def model_environment(self, tool: str | None = None) -> dict[str, str]:
        selected = tool or self.current_tool()
        env = self.model_config(selected).get("env", {})
        if not isinstance(env, dict):
            raise ConfigError(f"AI CLI {selected} 的 env 必须是对象")
        if not all(isinstance(key, str) and isinstance(value, (str, int, float, bool)) for key, value in env.items()):
            raise ConfigError(f"AI CLI {selected} 的 env 必须是字符串键和值")
        return {key: str(value) for key, value in env.items()}

    def version(self) -> str:
        root = self.base_dir().parent
        try:
            subject = subprocess.check_output(
                ["git", "-C", str(root), "log", "-1", "--pretty=%s"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return str(self.get("version", "unknown"))
        match = re.match(r"^(v\d+\.\d+\.\d+)\s+.+", subject)
        return match.group(1) if match else str(self.get("version", "unknown"))

    @staticmethod
    def _get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
        current: Any = data
        for part in dotted.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

    @staticmethod
    def _validate(server_data: dict[str, Any], model_data: dict[str, Any]) -> None:
        server_tools = Config._get(server_data, "ai.tools")
        model_tools = model_data.get("tools")
        default_tool = model_data.get("default_cli")
        if not isinstance(server_tools, dict) or not server_tools:
            raise ConfigError("config.json 的 ai.tools 必须是非空对象")
        if not isinstance(model_tools, dict) or not model_tools:
            raise ConfigError("model.json 的 tools 必须是非空对象")
        if set(server_tools) != set(model_tools):
            raise ConfigError("config.json 与 model.json 的工具列表必须一致")
        if not isinstance(default_tool, str) or default_tool not in server_tools:
            raise ConfigError("model.json 的 default_cli 必须指向已配置工具")
