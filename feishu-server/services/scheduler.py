from __future__ import annotations

import json
import logging
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
from config import Config
from services.ai_runner import AIRunner
from services.messenger import Messenger


logger = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    id: str
    name: str
    cron: str
    type: str
    receive_id: str
    receive_id_type: str = "open_id"
    prompt: str = ""
    command: str = ""
    enabled: bool = True
    tool: str | None = None
    model: str | None = None
    timeout_seconds: int = 120
    last_run_minute: str = ""
    last_result: str = ""
    run_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScheduledTask":
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "unnamed"),
            cron=str(data.get("cron") or "* * * * *"),
            type=str(data.get("type") or "agent"),
            receive_id=str(data.get("receive_id") or ""),
            receive_id_type=str(data.get("receive_id_type") or "open_id"),
            prompt=str(data.get("prompt") or ""),
            command=str(data.get("command") or ""),
            enabled=bool(data.get("enabled", True)),
            tool=data.get("tool"),
            model=data.get("model"),
            timeout_seconds=int(data.get("timeout_seconds") or 120),
            last_run_minute=str(data.get("last_run_minute") or ""),
            last_result=str(data.get("last_result") or ""),
            run_count=int(data.get("run_count") or 0),
            extra=dict(data.get("extra") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Scheduler:
    def __init__(self, config: Config, messenger: Messenger, ai_runner: AIRunner) -> None:
        self.config = config
        self.messenger = messenger
        self.ai_runner = ai_runner
        self.path = config.resolve_path("scheduler.data_file")
        self.port = int(config.get("scheduler.port", 8066))
        self.tick_seconds = int(config.get("scheduler.tick_seconds", 60))
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._tasks: dict[str, ScheduledTask] = {}
        self._server: ThreadingHTTPServer | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def start(self) -> None:
        threading.Thread(target=self._tick_loop, name="scheduler-tick", daemon=True).start()
        handler_cls = self._make_handler()
        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), handler_cls)
        threading.Thread(target=self._server.serve_forever, name="scheduler-http", daemon=True).start()
        logger.info("scheduler started on port %s", self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._server:
            self._server.shutdown()

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return [task.to_dict() for task in self._tasks.values()]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task.to_dict() if task else None

    def upsert_task(self, data: dict[str, Any]) -> dict[str, Any]:
        task = ScheduledTask.from_dict(data)
        with self._lock:
            self._tasks[task.id] = task
            self._save_locked()
        return task.to_dict()

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            existed = self._tasks.pop(task_id, None) is not None
            if existed:
                self._save_locked()
            return existed

    def run_task(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
        if not task:
            return False
        threading.Thread(target=self._execute_task, args=(task,), name=f"task-{task.id}", daemon=True).start()
        return True

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            now = datetime.now()
            minute_key = now.strftime("%Y%m%d%H%M")
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                if not task.enabled or task.last_run_minute == minute_key:
                    continue
                if cron_matches(task.cron, now):
                    task.last_run_minute = minute_key
                    self._save()
                    threading.Thread(target=self._execute_task, args=(task,), name=f"task-{task.id}", daemon=True).start()
            self._stop.wait(self.tick_seconds)

    def _execute_task(self, task: ScheduledTask) -> None:
        logger.info("running scheduled task id=%s type=%s", task.id, task.type)
        if task.type == "agent":
            result = self.ai_runner.run(
                {"user_input": task.prompt, "context": {"scheduled_task_id": task.id}},
                f"scheduled-{task.id}-{int(time.time())}",
                tool=task.tool,
                model=task.model,
            )
            card = cards.build_ai_card(result.tool, result.model, result.result or result.error, result.thinking)
            last_result = result.result or result.error
        elif task.type == "shell":
            try:
                completed = subprocess.run(
                    task.command,
                    shell=True,
                    executable="/bin/bash",
                    capture_output=True,
                    text=True,
                    timeout=task.timeout_seconds,
                    cwd=str(self.config.resolve_path("ai.workspace")),
                )
                output = (completed.stdout + "\n" + completed.stderr).strip()
                title = f"Shell task {task.name}"
                card = cards.build_generic_card(
                    title,
                    output or f"exit code: {completed.returncode}",
                    "green" if completed.returncode == 0 else "red",
                )
                last_result = output
            except subprocess.TimeoutExpired as exc:
                output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
                last_result = f"timeout after {task.timeout_seconds}s\n{output}".strip()
                card = cards.build_error_card("Shell 定时任务超时", last_result)
        else:
            card = cards.build_error_card("定时任务失败", f"不支持的任务类型：{task.type}")
            last_result = f"unsupported task type: {task.type}"

        self.messenger.send_card(task.receive_id, task.receive_id_type, card)
        with self._lock:
            stored = self._tasks.get(task.id)
            if stored:
                stored.run_count += 1
                stored.last_result = last_result[:2000]
                self._save_locked()

    def _load(self) -> None:
        if not self.path.exists():
            self._save()
            return
        with self.path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self._tasks = {task.id: task for task in (ScheduledTask.from_dict(item) for item in raw.get("tasks", []))}

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
                    self._json(200 if task else 404, task or {"error": "not found"})
                    return
                self._json(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path == "/api/tasks":
                    self._json(200, scheduler.upsert_task(self._body()))
                    return
                if self.path.startswith("/api/tasks/") and self.path.endswith("/run"):
                    task_id = self.path.split("/")[-2]
                    ok = scheduler.run_task(task_id)
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                self._json(404, {"error": "not found"})

            def do_PUT(self) -> None:
                if self.path.startswith("/api/tasks/"):
                    data = self._body()
                    data["id"] = self.path.rsplit("/", 1)[-1]
                    self._json(200, scheduler.upsert_task(data))
                    return
                self._json(404, {"error": "not found"})

            def do_DELETE(self) -> None:
                if self.path.startswith("/api/tasks/"):
                    task_id = self.path.rsplit("/", 1)[-1]
                    ok = scheduler.delete_task(task_id)
                    self._json(200 if ok else 404, {"ok": ok})
                    return
                self._json(404, {"error": "not found"})

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.info("scheduler api: " + fmt, *args)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                return json.loads(raw or "{}")

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler


def cron_matches(expr: str, dt: datetime) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    values = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    return all(_field_matches(part, value, low, high) for part, value, (low, high) in zip(parts, values, ranges))


def _field_matches(part: str, value: int, low: int, high: int) -> bool:
    if part == "*":
        return True
    for token in part.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("*/"):
            step = int(token[2:])
            if step > 0 and value % step == 0:
                return True
        elif "-" in token:
            start, end = (int(piece) for piece in token.split("-", 1))
            if start <= value <= end:
                return True
        elif token.isdigit():
            number = int(token)
            if value == number or (high == 7 and number == 7 and value == 0):
                return True
    return False
