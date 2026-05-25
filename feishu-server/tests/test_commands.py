import unittest

import commands


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


class CommandTests(unittest.TestCase):
    def _ctx(self, text: str, message=None, runner=None):
        return commands.CommandContext(
            text=text,
            message=message or {},
            config=FakeConfig(),
            session_store=None,
            ai_runner=runner or FakeRunner(),
            scheduler=None,
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


if __name__ == "__main__":
    unittest.main()
