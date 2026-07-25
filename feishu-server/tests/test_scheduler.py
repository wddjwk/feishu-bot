import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.ai_runner import AIResult
from services.scheduler import ScheduledTask, Scheduler, TaskValidationError, _combine_output, validate_cron


class FakeConfig:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_path(self, dotted: str) -> Path:
        values = {
            "server.scheduler.data_file": self.root / "data" / "tasks.json",
            "ai.workspace": self.root / "workspace",
        }
        return values[dotted]

    def get(self, dotted: str, default=None):
        values = {
            "server.scheduler.host": "127.0.0.1",
            "server.scheduler.port": 8066,
            "server.scheduler.tick_seconds": 60,
        }
        return values.get(dotted, default)

    def default_tool(self):
        return "default-cli"

    def default_model(self, _tool):
        return "default-model"

    def resolve_model(self, _tool, requested=None):
        return {"fast": "fast-model"}.get(requested, requested or "default-model")

    def tool_config(self, _tool):
        return {}

    def model_config(self, _tool):
        return {}


class FakeMessenger:
    def __init__(self) -> None:
        self.cards: list[dict] = []

    def send_card(self, receive_id, receive_id_type, card):
        self.cards.append({"receive_id": receive_id, "receive_id_type": receive_id_type, "card": card})


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, prompt, message_id, *, tool=None, model=None, timeout_seconds=None):
        self.calls.append(
            {
                "prompt": prompt,
                "message_id": message_id,
                "tool": tool,
                "model": model,
                "timeout_seconds": timeout_seconds,
            }
        )
        return AIResult(True, tool or "claude", model or "model-a", result="完成")


class SchedulerTests(unittest.TestCase):
    def _scheduler(self, root: Path) -> tuple[Scheduler, FakeMessenger, FakeRunner]:
        messenger = FakeMessenger()
        runner = FakeRunner()
        return Scheduler(FakeConfig(root), messenger, runner), messenger, runner

    def test_nested_agent_task_schema_runs_ai_with_its_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, messenger, runner = self._scheduler(Path(tmp))
            saved = scheduler.upsert_task(
                {
                    "id": "daily-summary",
                    "name": "每日总结",
                    "type": "agent",
                    "cron": "0 9 * * *",
                    "target": {"receive_id_type": "open_id", "receive_id": "ou_user"},
                    "payload": {"prompt": "总结今天", "tool": "claude", "model": "model-a"},
                    "timeout_seconds": 30,
                }
            )
            scheduler._execute_task(ScheduledTask.from_dict(saved))

        self.assertEqual(saved["target"]["receive_id"], "ou_user")
        self.assertEqual(saved["payload"]["prompt"], "总结今天")
        self.assertEqual(runner.calls[0]["timeout_seconds"], 30)
        self.assertEqual(runner.calls[0]["prompt"]["user_input"], "总结今天")
        self.assertEqual(runner.calls[0]["prompt"]["context"], [])
        self.assertEqual(messenger.cards[0]["receive_id"], "ou_user")

    def test_agent_task_uses_model_file_defaults_when_not_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, _messenger, runner = self._scheduler(Path(tmp))
            saved = scheduler.upsert_task(
                {
                    "id": "default-agent",
                    "name": "默认模型任务",
                    "type": "agent",
                    "cron": "0 9 * * *",
                    "target": {"receive_id": "ou_user"},
                    "payload": {"prompt": "执行每日检查"},
                }
            )
            scheduler._execute_task(ScheduledTask.from_dict(saved))

        self.assertEqual(runner.calls[0]["tool"], "default-cli")
        self.assertEqual(runner.calls[0]["model"], "default-model")

    def test_agent_task_resolves_explicit_model_alias_for_selected_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, _messenger, runner = self._scheduler(Path(tmp))
            saved = scheduler.upsert_task(
                {
                    "id": "custom-agent",
                    "name": "指定模型任务",
                    "type": "agent",
                    "cron": "0 9 * * *",
                    "target": {"receive_id": "ou_user"},
                    "payload": {"prompt": "执行检查", "tool": "claude", "model": "fast"},
                }
            )
            scheduler._execute_task(ScheduledTask.from_dict(saved))

        self.assertEqual(runner.calls[0]["tool"], "claude")
        self.assertEqual(runner.calls[0]["model"], "fast-model")

    def test_agent_task_uses_task_scoped_scheduler_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheduler, _messenger, runner = self._scheduler(root)
            saved = scheduler.upsert_task(
                {
                    "id": "owner-agent",
                    "name": "用户归属任务",
                    "type": "agent",
                    "cron": "0 9 * * *",
                    "target": {"receive_id": "ou_target"},
                    "payload": {"prompt": "完成检查"},
                }
            )
            scheduler._execute_task(ScheduledTask.from_dict(saved))

        self.assertEqual(runner.calls[0]["prompt"]["user_id"], "ou_target")
        self.assertEqual(runner.calls[0]["prompt"]["workfolder"], "scheduler/owner-agent")

    def test_rejects_non_string_agent_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, _messenger, _runner = self._scheduler(Path(tmp))
            with self.assertRaises(TaskValidationError):
                scheduler.upsert_task(
                    {
                        "id": "bad-model",
                        "type": "agent",
                        "cron": "0 9 * * *",
                        "target": {"receive_id": "ou_user"},
                        "payload": {"prompt": "检查", "model": 7},
                    }
                )

    def test_script_task_uses_argv_without_a_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "workspace" / "scheduler" / "report" / "report.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\necho report\n", encoding="utf-8")
            script.chmod(0o700)
            scheduler, messenger, _runner = self._scheduler(root)
            saved = scheduler.upsert_task(
                {
                    "id": "report",
                    "name": "报表",
                    "type": "script",
                    "cron": "0 * * * *",
                    "target": {"receive_id": "ou_user", "receive_id_type": "open_id"},
                    "payload": {"script": "scheduler/report/report.sh", "args": ["--today"]},
                }
            )

            class FakeProcess:
                pid = 12345
                returncode = 0

                def communicate(self, timeout=None):
                    return "报表完成", ""

            with patch("services.scheduler.subprocess.Popen", return_value=FakeProcess()) as popen:
                scheduler._execute_task(ScheduledTask.from_dict(saved))

        self.assertEqual(popen.call_args.args[0][-1], "--today")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(messenger.cards[0]["receive_id"], "ou_user")

    def test_rejects_shell_tasks_and_invalid_cron_at_save_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler, _messenger, _runner = self._scheduler(Path(tmp))
            with self.assertRaises(TaskValidationError):
                scheduler.upsert_task(
                    {
                        "id": "legacy",
                        "type": "shell",
                        "cron": "* * * * *",
                        "target": {"receive_id": "ou_user"},
                        "payload": {"command": "echo unsafe"},
                    }
                )
            with self.assertRaises(TaskValidationError):
                validate_cron("* bad * * *")

    def test_migrates_legacy_shell_task_to_an_executable_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "data" / "tasks.json"
            task_path.parent.mkdir(parents=True)
            task_path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "legacy-echo",
                                "name": "旧任务",
                                "type": "shell",
                                "cron": "0 9 * * *",
                                "receive_id": "ou_user",
                                "command": "echo legacy",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            scheduler, _messenger, _runner = self._scheduler(root)
            migrated = scheduler.get_task("legacy-echo")
            script_path = root / "workspace" / migrated["payload"]["script"]
            self.assertEqual(migrated["type"], "script")
            self.assertTrue(script_path.is_file())
            self.assertEqual(migrated["payload"]["script"], "scheduler/legacy-echo/legacy.sh")
            self.assertTrue(script_path.stat().st_mode & 0o111)
            self.assertNotIn("set -euo pipefail", script_path.read_text(encoding="utf-8"))

    def test_timeout_output_bytes_are_decoded(self):
        self.assertEqual(_combine_output(b"partial", b"\xff"), "partial\n\ufffd")

    def test_script_timeout_kills_the_entire_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "workspace" / "scheduler" / "slow" / "slow.sh"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env bash\nsleep 60\n", encoding="utf-8")
            script.chmod(0o700)
            scheduler, messenger, _runner = self._scheduler(root)
            saved = scheduler.upsert_task(
                {
                    "id": "slow",
                    "name": "慢脚本",
                    "type": "script",
                    "cron": "0 * * * *",
                    "timeout_seconds": 1,
                    "target": {"receive_id": "ou_user"},
                    "payload": {"script": "scheduler/slow/slow.sh"},
                }
            )

            class TimeoutProcess:
                pid = 12345
                returncode = -9

                def __init__(self):
                    self.calls = 0

                def communicate(self, timeout=None):
                    self.calls += 1
                    if self.calls == 1:
                        raise subprocess.TimeoutExpired("slow.sh", timeout or 1, output=b"partial", stderr=b"err")
                    return "partial", "err"

            with patch("services.scheduler.subprocess.Popen", return_value=TimeoutProcess()), patch(
                "services.scheduler.os.getpgid", return_value=12345
            ), patch("services.scheduler.os.killpg") as killpg:
                scheduler._execute_task(ScheduledTask.from_dict(saved))

        killpg.assert_called_once()
        self.assertEqual(messenger.cards[0]["card"]["header"]["template"], "red")


if __name__ == "__main__":
    unittest.main()
