from __future__ import annotations

import copy
import json
import re
import subprocess
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"


class ConfigError(RuntimeError):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class Config:
    def __init__(self, path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            raise ConfigError(f"config file not found: {self.path}")
        with self.path.open("r", encoding="utf-8") as fh:
            loaded = json.load(fh)
        self._data = _deep_merge({}, loaded)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def get(self, dotted: str, default: Any = None) -> Any:
        current: Any = self._data
        for part in dotted.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current

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
            raise ConfigError(f"missing path config: {dotted}")
        path = Path(str(value))
        if not path.is_absolute():
            path = self.base_dir() / path
        return path.resolve()

    def tools(self) -> list[str]:
        tools = self.get("ai.tools", {})
        return sorted(tools.keys()) if isinstance(tools, dict) else []

    def current_tool(self) -> str:
        tool = str(self.get("ai.tool", "")).strip()
        if tool not in self.tools():
            raise ConfigError(f"unknown ai.tool: {tool}")
        return tool

    def set_tool(self, tool: str) -> None:
        if tool not in self.tools():
            raise ConfigError(f"unknown AI tool: {tool}")
        self.set("ai.tool", tool)

    def tool_config(self, tool: str | None = None) -> dict[str, Any]:
        selected = tool or self.current_tool()
        cfg = self.get(f"ai.tools.{selected}")
        if not isinstance(cfg, dict):
            raise ConfigError(f"unknown AI tool: {selected}")
        return copy.deepcopy(cfg)

    def resolve_model(self, tool: str | None = None, requested: str | None = None) -> str:
        selected = tool or self.current_tool()
        tool_cfg = self.tool_config(selected)
        aliases = tool_cfg.get("aliases", {})
        current_models = self.get("ai.current_models", {})
        if requested:
            model = aliases.get(requested, requested)
        elif isinstance(current_models, dict) and current_models.get(selected):
            model = current_models[selected]
        else:
            model = aliases.get("default") or tool_cfg.get("default_model")
        if not model:
            raise ConfigError(f"no model configured for tool: {selected}")
        return str(model)

    def set_model(self, tool: str | None, requested: str) -> str:
        selected = tool or self.current_tool()
        model = self.resolve_model(selected, requested)
        self.set(f"ai.current_models.{selected}", model)
        return model

    def model_status(self) -> dict[str, str]:
        return {tool: self.resolve_model(tool) for tool in self.tools()}

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
