from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
import logger


class LarkCliError(RuntimeError):
    """Raised when lark-cli cannot complete a requested media operation."""


class LarkCliWrapper:
    """Small, bot-identity-only adapter for the lark-cli IM shortcuts."""

    def __init__(
        self,
        project_root: Path,
        workspace: Path,
        *,
        executable: str = "lark-cli",
        profile: str = "feishu-bot-server",
        timeout_seconds: int = 120,
    ) -> None:
        self.project_root = project_root.resolve()
        self.workspace = workspace.resolve()
        self.executable = executable
        self.profile = profile
        self.timeout_seconds = timeout_seconds
        self.env_path = self.project_root / ".env"
        self._auth_lock = threading.Lock()
        self._verified_app_id = ""

    @classmethod
    def from_config(cls, config: Any) -> "LarkCliWrapper":
        return cls(
            config.project_root(),
            config.resolve_path("ai.workspace"),
            executable=str(config.get("server.lark_cli.command", "lark-cli")),
            profile=str(config.get("server.lark_cli.profile", "feishu-bot-server")),
            timeout_seconds=int(config.get("server.lark_cli.timeout_seconds", 120)),
        )

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        resource_type: str,
        destination: Path,
    ) -> Path:
        if resource_type not in {"image", "file"}:
            raise LarkCliError(f"lark-cli 不支持的消息资源类型：{resource_type}")
        destination = self._workspace_file(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_bot_identity()
        payload = self._run_json(
            [
                "im",
                "+messages-resources-download",
                "--message-id",
                message_id,
                "--file-key",
                file_key,
                "--type",
                resource_type,
                "--output",
                f"./{destination.name}",
                "--as",
                "bot",
            ],
            cwd=destination.parent,
        )
        path = self._resolve_download_path(payload, destination)
        logger.info("lark-cli 已下载消息资源：消息=%s 资源=%s 路径=%s", message_id, file_key, path)
        return path

    def reply_file(self, message_id: str, file_path: Path, *, reply_in_thread: bool = False) -> dict[str, Any]:
        return self._reply_media(message_id, file_path, "--file", reply_in_thread=reply_in_thread)

    def reply_image(self, message_id: str, image_path: Path, *, reply_in_thread: bool = False) -> dict[str, Any]:
        return self._reply_media(message_id, image_path, "--image", reply_in_thread=reply_in_thread)

    def send_file(self, chat_id: str, file_path: Path) -> dict[str, Any]:
        return self._send_media(chat_id, file_path, "--file")

    def send_image(self, chat_id: str, image_path: Path) -> dict[str, Any]:
        return self._send_media(chat_id, image_path, "--image")

    def ensure_bot_identity(self) -> None:
        app_id, app_secret = self._credentials()
        with self._auth_lock:
            if self._verified_app_id == app_id:
                return
            status = self._bot_status()
            if not self._is_ready_for_app(status, app_id):
                logger.info("lark-cli 机器人身份未就绪，正在根据 .env 初始化应用配置：app_id=%s", app_id)
                self._run(
                    [
                        "config",
                        "init",
                        "--app-id",
                        app_id,
                        "--app-secret-stdin",
                        "--brand",
                        "feishu",
                        "--name",
                        self.profile,
                    ],
                    cwd=self.project_root,
                    stdin=f"{app_secret}\n",
                    use_profile=False,
                )
                status = self._bot_status()
            if not self._is_ready_for_app(status, app_id):
                raise LarkCliError("lark-cli 机器人身份初始化后仍不可用")
            self._verified_app_id = app_id

    def _reply_media(
        self,
        message_id: str,
        path: Path,
        media_flag: str,
        *,
        reply_in_thread: bool,
    ) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_file():
            raise LarkCliError(f"待发送的文件不存在：{path}")
        self.ensure_bot_identity()
        args = ["im", "+messages-reply", "--message-id", message_id, media_flag, f"./{path.name}", "--as", "bot"]
        if reply_in_thread:
            args.append("--reply-in-thread")
        payload = self._run_json(args, cwd=path.parent)
        result = self._result_data(payload)
        logger.info("lark-cli 已回复媒体消息：目标消息=%s 文件=%s", message_id, path)
        return result

    def _send_media(self, chat_id: str, path: Path, media_flag: str) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_file():
            raise LarkCliError(f"待发送的文件不存在：{path}")
        self.ensure_bot_identity()
        payload = self._run_json(
            ["im", "+messages-send", "--chat-id", chat_id, media_flag, f"./{path.name}", "--as", "bot"],
            cwd=path.parent,
        )
        result = self._result_data(payload)
        logger.info("lark-cli 已发送媒体消息：群=%s 文件=%s", chat_id, path)
        return result

    def _bot_status(self) -> dict[str, Any] | None:
        try:
            stdout = self._run(["auth", "status", "--json", "--verify"], cwd=self.project_root)
            payload = json.loads(stdout)
        except (LarkCliError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_ready_for_app(status: dict[str, Any] | None, app_id: str) -> bool:
        if not isinstance(status, dict) or status.get("appId") != app_id:
            return False
        identities = status.get("identities")
        if not isinstance(identities, dict):
            return False
        bot = identities.get("bot")
        return isinstance(bot, dict) and bot.get("status") == "ready" and bot.get("verified") is True

    def _credentials(self) -> tuple[str, str]:
        values = dotenv_values(self.env_path)
        app_id = str(values.get("FEISHU_APP_ID") or "").strip()
        app_secret = str(values.get("FEISHU_APP_SECRET") or "").strip()
        if not app_id or not app_secret:
            raise LarkCliError(".env 中缺少 FEISHU_APP_ID 或 FEISHU_APP_SECRET，无法初始化 lark-cli")
        return app_id, app_secret

    def _run_json(self, args: list[str], *, cwd: Path) -> dict[str, Any]:
        stdout = self._run(args, cwd=cwd)
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise LarkCliError(f"lark-cli 未返回有效 JSON：{stdout[:300]}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise LarkCliError(self._payload_error(payload))
        return payload

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        stdin: str | None = None,
        use_profile: bool = True,
    ) -> str:
        if not shutil.which(self.executable):
            raise LarkCliError(f"未找到 lark-cli 命令：{self.executable}")
        command = [self.executable]
        if use_profile:
            command.extend(["--profile", self.profile])
        command.extend(args)
        try:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=self._environment(),
            )
        except subprocess.TimeoutExpired as exc:
            logger.error("lark-cli 执行超时（%ss）：%s", self.timeout_seconds, " ".join(args[:3]))
            raise LarkCliError(f"lark-cli 执行超时：{' '.join(args[:3])}") from exc
        if completed.returncode != 0:
            logger.error(
                "lark-cli 退出码异常：%s 退出码=%s stderr=%s",
                " ".join(args[:3]),
                completed.returncode,
                completed.stderr[:300] if completed.stderr else "（空）",
            )
            raise LarkCliError(self._error_text(completed.stderr, completed.stdout))
        return completed.stdout

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        return env

    def _workspace_file(self, path: Path) -> Path:
        resolved = path.resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise LarkCliError(f"lark-cli 文件路径必须位于 agent-workspace：{path}") from exc
        return resolved

    @staticmethod
    def _result_data(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def _resolve_download_path(self, payload: dict[str, Any], destination: Path) -> Path:
        for candidate in self._paths_in_payload(payload):
            path = Path(candidate)
            if not path.is_absolute():
                path = destination.parent / path
            try:
                path = self._workspace_file(path)
            except LarkCliError:
                continue
            if path.is_file():
                return path
        if destination.is_file():
            return destination
        inferred = sorted(destination.parent.glob(f"{destination.name}.*"), key=lambda path: path.stat().st_mtime, reverse=True)
        if inferred:
            return self._workspace_file(inferred[0])
        raise LarkCliError(f"lark-cli 未在预期位置保存下载文件：{destination}")

    @staticmethod
    def _paths_in_payload(value: Any) -> list[str]:
        paths: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "local_path", "output", "output_path"} and isinstance(item, str):
                    paths.append(item)
                paths.extend(LarkCliWrapper._paths_in_payload(item))
        elif isinstance(value, list):
            for item in value:
                paths.extend(LarkCliWrapper._paths_in_payload(item))
        return paths

    @staticmethod
    def _payload_error(payload: Any) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return f"lark-cli 调用失败：{error['message']}"
            if isinstance(payload.get("message"), str):
                return f"lark-cli 调用失败：{payload['message']}"
        return "lark-cli 调用失败"

    @staticmethod
    def _error_text(stderr: str, stdout: str) -> str:
        for raw in (stderr, stdout):
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                return f"lark-cli 调用失败：{raw[:500]}"
            return LarkCliWrapper._payload_error(payload)
        return "lark-cli 调用失败"
