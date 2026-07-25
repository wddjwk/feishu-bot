from __future__ import annotations

import copy
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import cards
from config import Config, ConfigError
from services.ai_runner import AIRunner, format_thinking
from services.messenger import FeishuApiError, Messenger
import logger

VALID_TASK_TYPES = {"agent", "script"}


class TaskValidationError(ValueError):
    pass


@dataclass
class ScheduledTask:
    id: str
    name: str
    cron: str
    type: str
    receive_id: str
    receive_id_type: str = "open_id"
    prompt: str = ""
    script: str = ""
    args: list[str] = field(default_factory=list)
    tool: str | None = None
    model: str | None = None
    timeout_seconds: int = 120
    enabled: bool = True
    last_run_minute: str = ""
    last_result: str = ""
    run_count: int = 0
    legacy_command: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        target_value = data.get("target")
        target: dict[str, Any] = target_value if isinstance(target_value, dict) else {}
        payload_value = data.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        args = payload.get("args", data.get("args", []))
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise TaskValidationError("脚本任务的 payload.args 必须是字符串数组")
        try:
            timeout_seconds = int(data.get("timeout_seconds") or 120)
            run_count = int(data.get("run_count") or 0)
        except (TypeError, ValueError) as exc:
            raise TaskValidationError("任务 timeout_seconds 和 run_count 必须是整数") from exc
        extra = data.get("extra") or {}
        if not isinstance(extra, dict):
            raise TaskValidationError("任务 extra 必须是对象")
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "未命名"),
            cron=str(data.get("cron") or "* * * * *"),
            type=str(data.get("type") or "agent"),
            receive_id=str(target.get("receive_id", data.get("receive_id") or "")),
            receive_id_type=str(target.get("receive_id_type", data.get("receive_id_type") or "open_id")),
            prompt=str(payload.get("prompt", data.get("prompt") or "")),
            script=str(payload.get("script", data.get("script") or "")),
            args=list(args),
            tool=_optional_string(payload.get("tool", data.get("tool")), "payload.tool"),
            model=_optional_string(payload.get("model", data.get("model")), "payload.model"),
            timeout_seconds=timeout_seconds,
            enabled=bool(data.get("enabled", True)),
            last_run_minute=str(data.get("last_run_minute") or ""),
            last_result=str(data.get("last_result") or ""),
            run_count=run_count,
            legacy_command=str(payload.get("command", data.get("command") or "")),
            extra=dict(extra),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any]
        if self.type == "agent":
            payload = {"prompt": self.prompt}
            if self.tool:
                payload["tool"] = self.tool
            if self.model:
                payload["model"] = self.model
        elif self.type == "script":
            payload = {"script": self.script, "args": list(self.args)}
        else:
            payload = {"command": self.legacy_command}
        return {
            "id": self.id,
            "name": self.name,
            "cron": self.cron,
            "type": self.type,
            "target": {
                "receive_id": self.receive_id,
                "receive_id_type": self.receive_id_type,
            },
            "payload": payload,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "last_run_minute": self.last_run_minute,
            "last_result": self.last_result,
            "run_count": self.run_count,
            "extra": dict(self.extra),
        }


class Scheduler:
    def __init__(self, config: Config, messenger: Messenger, ai_runner: AIRunner) -> None:
        self.config = config
        self.messenger = messenger
        self.ai_runner = ai_runner
        self.path = config.resolve_path("server.scheduler.data_file")
        self.workspace = config.resolve_path("ai.workspace")
        self.script_root = self.workspace / "scheduler"
        self.host = str(config.get("server.scheduler.host", "127.0.0.1"))
        self.port = int(config.get("server.scheduler.port", 8066))
        self.tick_seconds = int(config.get("server.scheduler.tick_seconds", 60))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._tasks: dict[str, ScheduledTask] = {}
        self._server: ThreadingHTTPServer | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._load()

    def start(self) -> None:
        handler_cls = self._make_handler()
        self._server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        threading.Thread(target=self._tick_loop, name="scheduler-tick", daemon=True).start()
        threading.Thread(target=self._server.serve_forever, name="scheduler-http", daemon=True).start()
        logger.info("调度器已启动：地址=http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        logger.info("调度器已停止")

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [task.to_dict() for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def upsert_task(self, data: dict[str, Any]) -> dict[str, Any]:
        task_id = str(data.get("id") or "")
        with self._lock:
            existing = self._tasks.get(task_id) if task_id else None
            merged = _deep_merge(existing.to_dict(), data) if existing else data
            task = ScheduledTask.from_dict(merged)
            self._validate_task(task)
            self._tasks[task.id] = task
            self._save_locked()
        logger.info("已保存定时任务：ID=%s 名称=%s 类型=%s Cron=%s", task.id, task.name, task.type, task.cron)
        return task.to_dict()

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            existed = self._tasks.pop(task_id, None) is not None
            if existed:
                self._save_locked()
        if existed:
            logger.info("已删除定时任务：ID=%s", task_id)
        return existed

    def run_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            snapshot = ScheduledTask.from_dict(task.to_dict()) if task else None
        if not snapshot:
            return False
        threading.Thread(target=self._execute_task, args=(snapshot,), name=f"task-{snapshot.id}", daemon=True).start()
        return True

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            minute_key = now.strftime("%Y%m%d%H%M")
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                if not task.enabled or task.last_run_minute == minute_key or not cron_matches(task.cron, now):
                    continue
                with self._lock:
                    stored = self._tasks.get(task.id)
                    if not stored or not stored.enabled or stored.last_run_minute == minute_key:
                        continue
                    stored.last_run_minute = minute_key
                    self._save_locked()
                    snapshot = ScheduledTask.from_dict(stored.to_dict())
                threading.Thread(target=self._execute_task, args=(snapshot,), name=f"task-{snapshot.id}", daemon=True).start()
            self._stop.wait(self.tick_seconds)

    def _execute_task(self, task: ScheduledTask) -> None:
        logger.info("开始执行定时任务：ID=%s 名称=%s 类型=%s", task.id, task.name, task.type)
        if task.type == "agent":
            card, last_result = self._execute_agent_task(task)
        elif task.type == "script":
            card, last_result = self._execute_script_task(task)
        else:
            card = cards.build_error_card("定时任务失败", f"不支持的任务类型：{task.type}")
            last_result = f"不支持的任务类型：{task.type}"

        try:
            self.messenger.send_card(task.receive_id, task.receive_id_type, card)
            logger.info("定时任务结果已发送：ID=%s 接收方=%s", task.id, task.receive_id)
        except FeishuApiError as exc:
            logger.exception("发送定时任务结果失败：ID=%s 错误=%s", task.id, exc)
            last_result = f"{last_result}\n发送结果失败：{exc}".strip()
        finally:
            with self._lock:
                stored = self._tasks.get(task.id)
                if stored:
                    stored.run_count += 1
                    stored.last_result = last_result[:2000]
                    self._save_locked()
        logger.info("定时任务执行结束：ID=%s 结果字符=%s", task.id, len(last_result))

    def _execute_agent_task(self, task: ScheduledTask) -> tuple[dict[str, Any], str]:
        try:
            tool = task.tool or self.config.default_tool()
            model = self.config.resolve_model(tool, task.model) if task.model else self.config.default_model(tool)
        except (ConfigError, AttributeError) as exc:
            result = f"无法解析 AI CLI 或模型：{exc}"
            return cards.build_error_card("AI 定时任务失败", result), result
        logger.info(
            "开始执行 AI 定时任务：ID=%s CLI=%s 模型=%s 超时=%ss",
            task.id,
            tool,
            model,
            task.timeout_seconds,
        )
        result = self.ai_runner.run(
            {
                "workfolder": self._task_dir_relative(task.id),
                "user_id": task.receive_id if task.receive_id_type == "open_id" else "scheduler",
                "user_input": task.prompt,
                "context": [],
            },
            f"scheduled-{task.id}-{int(time.time())}",
            tool=tool,
            model=model,
            timeout_seconds=task.timeout_seconds,
        )
        if result.ok:
            return cards.build_ai_card(
                result.tool,
                result.model,
                result.result,
                format_thinking(result.parts, self.config),
                usage=result.usage if self.config.get("options.features.show_token_usage_on_card", True) else None,
            ), result.result
        return cards.build_error_card("AI 定时任务失败", result.error or "未能获取有效输出", result.stderr), result.error or result.stderr

    def _execute_script_task(self, task: ScheduledTask) -> tuple[dict[str, Any], str]:
        try:
            script_path = self._resolve_script_path(task)
        except TaskValidationError as exc:
            return cards.build_error_card("脚本定时任务失败", str(exc)), str(exc)
        command = [str(script_path), *task.args]
        logger.info("开始执行脚本定时任务：ID=%s 超时=%ss 命令=%s", task.id, task.timeout_seconds, shlex.join(command))
        started_at = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(self.workspace),
                start_new_session=True,
            )
        except OSError as exc:
            result = f"启动脚本失败：{exc}"
            logger.exception("脚本定时任务启动失败：ID=%s 错误=%s", task.id, exc)
            return cards.build_error_card("脚本定时任务失败", result), result
        try:
            stdout, stderr = process.communicate(timeout=task.timeout_seconds)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            output = _combine_output(stdout, stderr)
            result = f"脚本在 {task.timeout_seconds} 秒后超时\n{output}".strip()
            logger.error("脚本定时任务超时：ID=%s 原始输出：\n%s", task.id, output or "（空）")
            return cards.build_error_card("脚本定时任务超时", result), result

        output = _combine_output(stdout, stderr)
        elapsed = time.monotonic() - started_at
        logger.info(
            "脚本定时任务结束：ID=%s 退出码=%s 耗时=%.2fs 标准输出：\n%s\n错误输出：\n%s",
            task.id,
            process.returncode,
            elapsed,
            stdout or "（空）",
            stderr or "（空）",
        )
        if process.returncode == 0:
            result = output or f"脚本执行成功，退出码：{process.returncode}"
            return cards.build_generic_card(f"脚本任务：{task.name}", result, "green"), result
        result = output or f"脚本退出码异常：{process.returncode}"
        return cards.build_error_card(f"脚本任务失败：{task.name}", result), result

    def _load(self) -> None:
        if not self.path.exists():
            self._save()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("无法读取定时任务文件 %s：%s", self.path, exc)
            return
        items = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            logger.error("定时任务文件格式无效：%s", self.path)
            return

        changed = False
        tasks: dict[str, ScheduledTask] = {}
        for item in items:
            if not isinstance(item, dict):
                logger.warning("跳过非对象格式的定时任务")
                changed = True
                continue
            task: ScheduledTask | None = None
            try:
                task = ScheduledTask.from_dict(item)
                if task.type == "shell":
                    self._migrate_legacy_shell_task(task)
                    changed = True
                self._validate_task(task)
            except TaskValidationError as exc:
                task_id = str(item.get("id") or "未知")
                logger.warning("禁用无效定时任务：ID=%s 错误=%s", task_id, exc)
                if task is None:
                    try:
                        task = ScheduledTask.from_dict(item)
                    except TaskValidationError:
                        changed = True
                        continue
                task.enabled = False
                task.extra["validation_error"] = str(exc)
                changed = True
            tasks[task.id] = task
        with self._lock:
            self._tasks = tasks
            if changed:
                self._save_locked()
        logger.info("已加载定时任务：数量=%s 文件=%s", len(tasks), self.path)

    def _migrate_legacy_shell_task(self, task: ScheduledTask) -> None:
        if not task.legacy_command.strip():
            raise TaskValidationError("旧 shell 任务缺少 command，无法迁移为脚本任务")
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "-", task.id).strip("-") or uuid.uuid4().hex[:12]
        relative_path = Path("scheduler") / safe_id / "legacy.sh"
        script_path = (self.workspace / relative_path).resolve()
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "#!/usr/bin/env bash\n\n" + task.legacy_command.rstrip() + "\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)
        task.type = "script"
        task.script = relative_path.as_posix()
        task.args = []
        task.extra["migrated_from"] = "shell"
        task.legacy_command = ""
        logger.warning("已将旧 shell 定时任务迁移为受限脚本：ID=%s 脚本=%s", task.id, task.script)

    def _validate_task(self, task: ScheduledTask) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", task.id):
            raise TaskValidationError("任务 ID 只能包含字母、数字、下划线和连字符，且必须以字母或数字开头")
        if task.type not in VALID_TASK_TYPES:
            raise TaskValidationError("任务类型只能是 agent 或 script；旧 shell 任务需迁移为 script")
        if not task.receive_id:
            raise TaskValidationError("任务 target.receive_id 不能为空")
        if task.timeout_seconds <= 0:
            raise TaskValidationError("任务 timeout_seconds 必须大于 0")
        validate_cron(task.cron)
        if task.type == "agent" and not task.prompt.strip():
            raise TaskValidationError("AI 任务的 payload.prompt 不能为空")
        if task.type == "agent" and task.tool:
            try:
                self.config.tool_config(task.tool)
                self.config.model_config(task.tool)
            except ConfigError as exc:
                raise TaskValidationError(f"AI 任务的 payload.tool 无效：{task.tool}") from exc
        if task.type == "script":
            script_path = self._resolve_script_path(task)
            if not script_path.is_file():
                raise TaskValidationError(f"脚本不存在或不是文件：{task.script}")
            if not script_path.stat().st_mode & 0o111:
                raise TaskValidationError(f"脚本不可执行：{task.script}")

    def _resolve_script_path(self, task: ScheduledTask) -> Path:
        if not task.script:
            raise TaskValidationError("脚本任务的 payload.script 不能为空")
        relative_path = Path(task.script)
        if relative_path.is_absolute():
            raise TaskValidationError("脚本路径必须相对于 agent-workspace")
        expected_root = Path("scheduler") / _safe_task_id(task.id)
        if relative_path.parts[:2] != expected_root.parts:
            raise TaskValidationError(f"脚本必须位于 {expected_root.as_posix()}/ 目录下")
        workspace = self.workspace.resolve()
        candidate = (workspace / relative_path).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError as exc:
            raise TaskValidationError("脚本路径不能离开 agent-workspace") from exc
        return candidate

    def _task_dir(self, task_id: str) -> Path:
        task_dir = self.script_root / _safe_task_id(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir.resolve()

    @staticmethod
    def _task_dir_relative(task_id: str) -> str:
        return (Path("scheduler") / _safe_task_id(task_id)).as_posix()

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except ProcessLookupError:
            return

    def _save(self) -> None:
        with self._lock:
            self._save_locked()

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump({"tasks": [task.to_dict() for task in self._tasks.values()]}, fh, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        scheduler = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/health":
                    self._json(200, {"ok": True})
                    return
                if self.path == "/api/tasks":
                    self._json(200, {"tasks": scheduler.list_tasks()})
                    return
                if self.path.startswith("/api/tasks/"):
                    task_id = self.path.rsplit("/", 1)[-1]
                    task = scheduler.get_task(task_id)
                    self._json(200 if task else 404, task or {"error": "未找到任务"})
                    return
                self._json(404, {"error": "未找到接口"})

            def do_POST(self) -> None:
                try:
                    if self.path == "/api/tasks":
                        self._json(200, scheduler.upsert_task(self._body()))
                        return
                    if self.path.startswith("/api/tasks/") and self.path.endswith("/run"):
                        task_id = self.path.split("/")[-2]
                        ok = scheduler.run_task(task_id)
                        self._json(200 if ok else 404, {"ok": ok})
                        return
                except (TaskValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(404, {"error": "未找到接口"})

            def do_PUT(self) -> None:
                try:
                    if self.path.startswith("/api/tasks/"):
                        data = self._body()
                        data["id"] = self.path.rsplit("/", 1)[-1]
                        self._json(200, scheduler.upsert_task(data))
                        return
                except (TaskValidationError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(404, {"error": "未找到接口"})

            def do_DELETE(self) -> None:
                if self.path.startswith("/api/tasks/"):
                    task_id = self.path.rsplit("/", 1)[-1]
                    ok = scheduler.delete_task(task_id)
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                self._json(404, {"error": "未找到接口"})

            def log_message(self, format: str, *args: Any) -> None:
                logger.info("调度器 API 访问：" + format, *args)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                data = json.loads(raw or "{}")
                if not isinstance(data, dict):
                    raise TaskValidationError("请求体必须是 JSON 对象")
                return data

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


def validate_cron(expr: str) -> None:
    parts = expr.split()
    if len(parts) != 5:
        raise TaskValidationError("Cron 必须包含 5 个字段：分 时 日 月 周")
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    for part, (low, high) in zip(parts, ranges):
        _validate_cron_field(part, low, high)


def cron_matches(expr: str, dt: datetime) -> bool:
    try:
        validate_cron(expr)
    except TaskValidationError:
        return False
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    highs = [59, 23, 31, 12, 7]
    return all(_field_matches(part, value, high) for part, value, high in zip(expr.split(), values, highs))


def _validate_cron_field(part: str, low: int, high: int) -> None:
    if not part:
        raise TaskValidationError("Cron 字段不能为空")
    for token in part.split(","):
        if token == "*":
            continue
        if token.startswith("*/"):
            step = token[2:]
            if not step.isdigit() or int(step) <= 0:
                raise TaskValidationError(f"无效的 Cron 步长：{token}")
            continue
        if "-" in token:
            pieces = token.split("-", 1)
            if not all(piece.isdigit() for piece in pieces):
                raise TaskValidationError(f"无效的 Cron 范围：{token}")
            start, end = (int(piece) for piece in pieces)
            if start < low or end > high or start > end:
                raise TaskValidationError(f"Cron 范围超出允许值：{token}")
            continue
        if not token.isdigit() or not low <= int(token) <= high:
            raise TaskValidationError(f"Cron 值超出允许范围：{token}")


def _field_matches(part: str, value: int, high: int) -> bool:
    if part == "*":
        return True
    for token in part.split(","):
        if token.startswith("*/"):
            if value % int(token[2:]) == 0:
                return True
        elif "-" in token:
            start, end = (int(piece) for piece in token.split("-", 1))
            if start <= value <= end:
                return True
        else:
            number = int(token)
            if value == number or (high == 7 and number == 7 and value == 0):
                return True
    return False


def _combine_output(
    stdout: str | bytes | bytearray | memoryview | None,
    stderr: str | bytes | bytearray | memoryview | None,
) -> str:
    return "\n".join(part for part in (_as_text(stdout), _as_text(stderr)) if part).strip()


def _as_text(value: str | bytes | bytearray | memoryview | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return ""


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskValidationError(f"{field} 必须是字符串")
    return value.strip() or None


def _safe_task_id(task_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", task_id).strip("._") or "task"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
