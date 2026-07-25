import unittest

import cards
import commands
from config import Config


class FakeConfig:
    pass


class FakeRunner:
    def __init__(self) -> None:
        self.killed: list[str] = []

    def running(self):
        return [
            {
                "message_id": "om_abcdef",
                "pid": 123,
                "tool": "claude",
                "model": "deepseek-v4-flash",
                "elapsed_seconds": 9,
            }
        ]

    def kill(self, target: str):
        self.killed.append(target)
        return "om_abcdef" if target in {"om_abcdef", "om_"} else None


class FakeScheduler:
    def list_tasks(self):
        return [{"id": "daily", "name": "每日任务"}]


class CommandTests(unittest.TestCase):
    def _ctx(self, text: str, message=None, runner=None, config=None, scheduler=None):
        return commands.CommandContext(
            text=text,
            message=message or {},
            config=config or FakeConfig(),
            session_store=None,
            ai_runner=runner or FakeRunner(),
            scheduler=scheduler,
            messenger=None,
        )

    def test_running_lists_active_tasks(self):
        response = commands.registry.match(self._ctx("/running"))
        self.assertIsNotNone(response)
        self.assertEqual(response.card["header"]["title"]["content"], "运行中的 AI 分析")
        self.assertIn("om_abcdef", response.card["body"]["elements"][0]["content"])

    def test_kill_accepts_explicit_id_and_returns_red_card(self):
        runner = FakeRunner()
        response = commands.registry.match(self._ctx("/kill om_", runner=runner))
        self.assertEqual(runner.killed, ["om_"])
        self.assertEqual(response.card["header"]["template"], "red")

    def test_bare_kill_uses_replied_parent_id(self):
        runner = FakeRunner()
        response = commands.registry.match(self._ctx("/kill", message={"parent_id": "om_abcdef"}, runner=runner))
        self.assertEqual(runner.killed, ["om_abcdef"])
        self.assertIn("已终止", response.card["header"]["title"]["content"])

    def test_cli_lists_tools_and_switches_to_the_tool_default_model(self):
        config = Config()

        listed = commands.registry.match(self._ctx("/cli", config=config))
        switched = commands.registry.match(self._ctx("/cli claude", config=config))

        self.assertIn("codebuddy", listed.card["body"]["elements"][0]["content"])
        self.assertEqual(config.current_tool(), "claude")
        self.assertEqual(config.resolve_model(), config.default_model("claude"))
        self.assertIn("claude-sonnet-5", switched.card["body"]["elements"][0]["content"])

    def test_cli_rejects_unknown_tool_without_changing_selection(self):
        config = Config()
        original = config.current_tool()

        response = commands.registry.match(self._ctx("/cli unknown", config=config))

        self.assertEqual(config.current_tool(), original)
        self.assertEqual(response.card["header"]["template"], "red")

    def test_model_lists_aliases_and_accepts_arbitrary_override(self):
        config = Config()
        config.set_tool("codebuddy")

        listed = commands.registry.match(self._ctx("/model", config=config))
        switched = commands.registry.match(self._ctx("/model custom-model", config=config))

        self.assertIn("deepseek-v4-pro", listed.card["body"]["elements"][0]["content"])
        self.assertEqual(config.resolve_model(), "custom-model")
        self.assertIn("custom-model", switched.card["body"]["elements"][0]["content"])

    def test_status_includes_running_and_scheduled_task_counts(self):
        response = commands.registry.match(
            self._ctx("/status", config=Config(), scheduler=FakeScheduler())
        )

        content = response.card["body"]["elements"][0]["content"]
        self.assertIn("正在运行：`1`", content)
        self.assertIn("定时任务：`1`", content)

    def test_reload_keeps_runtime_selection_and_applies_card_icons(self):
        config = Config()
        config.set_tool("claude")
        config.set_model(None, "runtime-model")

        response = commands.registry.match(self._ctx("/reload", config=config))
        card = cards.build_ai_card("codebuddy", "model", "done")

        self.assertEqual(config.current_tool(), "claude")
        self.assertEqual(config.resolve_model(), "runtime-model")
        self.assertEqual(card["header"]["title"]["content"], "🧑‍🎤Codebuddy model")
        self.assertEqual(response.card["header"]["template"], "green")


if __name__ == "__main__":
    unittest.main()
