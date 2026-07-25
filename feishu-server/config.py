from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import logger


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
DEFAULT_USER_CONFIG_PATH = PROJECT_ROOT / "config.json"

LAUNCH_FIELDS = frozenset(
    {
        "command",
        "base_args",
        "session_args",
        "resume_args",
        "prompt_transport",
        "output_parser",
    }
)
MODEL_FIELDS = frozenset(
    {
        "default_model",
        "model_list",
        "aliases",
        "env",
    }
)


class ConfigError(RuntimeError):
    pass


def _strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i < n:
                if i + 1 < n and text[i] == "*" and text[i + 1] == "/":
                    i += 2
                    break
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    in_string = False
    escape = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
            out.append(ch)
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"{label} 不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = fh.read()
        data = json.loads(_strip_trailing_commas(_strip_comments(raw)))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} 不是有效 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{label} 根节点必须是 JSON 对象")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    def __init__(
        self,
        path: Path | str = DEFAULT_CONFIG_PATH,
        user_path: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.user_path = Path(user_path) if user_path else None
        self._data: dict[str, Any] = {}
        self._runtime_tool: str | None = None
        self._runtime_models: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        data = _read_json(self.path, "config.json")
        if self.user_path and self.user_path.exists():
            user_data = _read_json(self.user_path, "用户 config.json")
            data = _deep_merge(data, user_data)
        self._validate(data)
        self._data = data
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
            self.default_tool(),
        )

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

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

    def project_root(self) -> Path:
        return self.path.parent.parent

    def resolve_path(self, dotted: str) -> Path:
        value = self.get(dotted)
        if not value:
            raise ConfigError(f"缺少路径配置：{dotted}")
        path = Path(str(value))
        if not path.is_absolute():
            path = self.project_root() / path
        return path.resolve()

    def tools(self) -> list[str]:
        tools = self.get("ai.tools", {})
        if not isinstance(tools, dict):
            return []
        return sorted(tools)

    def default_tool(self) -> str:
        tool = str(self.get("ai.default_tool") or "").strip()
        if tool not in self.tools():
            raise ConfigError(f"ai.default_tool 无效：{tool}")
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

    def _raw_tool(self, tool: str) -> dict[str, Any]:
        raw = self._get(self._data, f"ai.tools.{tool}")
        if not isinstance(raw, dict):
            raise ConfigError(f"缺少 AI CLI 配置：{tool}")
        return raw

    def tool_config(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        raw = self._raw_tool(selected)
        return {k: copy.deepcopy(v) for k, v in raw.items() if k in LAUNCH_FIELDS}

    def model_config(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        raw = self._raw_tool(selected)
        return {k: copy.deepcopy(v) for k, v in raw.items() if k in MODEL_FIELDS}

    def tool_icons(self) -> dict[str, str]:
        tools = self.get("ai.tools", {})
        if not isinstance(tools, dict):
            return {}
        icons: dict[str, str] = {}
        for name, cfg in tools.items():
            if not isinstance(cfg, dict):
                continue
            icon = cfg.get("icon")
            if isinstance(icon, str) and icon.strip():
                icons[name] = icon
        return icons

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
        if not isinstance(aliases, dict) or not all(
            isinstance(name, str) and isinstance(model, str) for name, model in aliases.items()
        ):
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
        root = self.project_root()
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
    def _validate(data: dict[str, Any]) -> None:
        tools = Config._get(data, "ai.tools")
        default_tool = Config._get(data, "ai.default_tool")
        if not isinstance(tools, dict) or not tools:
            raise ConfigError("config.json 的 ai.tools 必须是非空对象")
        for name, cfg in tools.items():
            if not isinstance(cfg, dict):
                raise ConfigError(f"config.json 的 ai.tools.{name} 必须是对象")
        if not isinstance(default_tool, str) or default_tool not in tools:
            raise ConfigError("config.json 的 ai.default_tool 必须指向已配置工具")
