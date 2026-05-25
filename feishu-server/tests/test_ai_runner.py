import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services.ai_runner import AIRunner, parse_output


class ParseOutputTests(unittest.TestCase):
    def test_claude_result_event(self):
        stdout = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"checking"}]},"session_id":"s1"}',
                '{"type":"result","result":"final answer","usage":{"input_tokens":3,"output_tokens":4}}',
            ]
        )
        result = parse_output("claude", stdout)
        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.result, "final answer")
        self.assertIn("🧠 Thinking", result.thinking)
        self.assertEqual(result.usage.input_tokens, 3)
        self.assertEqual(result.usage.output_tokens, 4)

    def test_claude_tool_and_skill_process_rendering(self):
        stdout = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"skill","input":{"skill":"scheduler"}},{"type":"tool_use","name":"bash","input":{"command":"curl /api/tasks"}}]}}',
                '{"type":"result","result":"ok"}',
            ]
        )
        result = parse_output("claude", stdout)
        self.assertIn("📚 Skill scheduler", result.thinking)
        self.assertIn("🛠 Tool bash", result.thinking)
        self.assertEqual(result.result, "ok")

    def test_copilot_reasoning_delta_not_duplicated(self):
        stdout = "\n".join(
            [
                '{"type":"reasoning_delta","delta":"step 1"}',
                '{"type":"reasoning","content":"full duplicate"}',
                '{"type":"result","result":"done","session_id":"abc"}',
            ]
        )
        result = parse_output("copilot", stdout)
        self.assertEqual(result.thinking, "step 1")
        self.assertEqual(result.result, "done")
        self.assertEqual(result.session_id, "abc")

    def test_copilot_content_delta_is_joined(self):
        stdout = "\n".join(
            [
                '{"type":"content_delta","delta":"hel"}',
                '{"type":"content_delta","delta":"lo"}',
            ]
        )
        result = parse_output("copilot", stdout)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "hello")

    def test_copilot_cli_json_events(self):
        stdout = "\n".join(
            [
                '{"type":"assistant.message_delta","data":{"deltaContent":"O"}}',
                '{"type":"assistant.message_delta","data":{"deltaContent":"K"}}',
                '{"type":"assistant.message","data":{"content":"OK","outputTokens":4}}',
                '{"type":"result","sessionId":"s1","exitCode":0}',
            ]
        )
        result = parse_output("copilot", stdout)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "OK")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.usage.output_tokens, 4)

    def test_plain_stdout_fallback(self):
        result = parse_output("qoder", "hello\nworld\n")
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "world")

    def test_json_error_event_is_failure(self):
        stdout = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"text","text":"rate limited"}]}}',
                '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["rate limited"]}',
            ]
        )
        result = parse_output("codebuddy", stdout)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rate limited")

    def test_non_stdin_tool_gets_prompt_as_argument(self):
        calls = {}

        class FakeConfig:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def current_tool(self):
                return "codebuddy"

            def resolve_model(self, tool):
                return "deepseek-v4-flash"

            def tool_config(self, tool):
                return {
                    "command": "codebuddy",
                    "args": ["--print", "--model", "{model}", "--output-format", "stream-json"],
                    "resume_args": ["--resume", "{session_id}"],
                    "stdin": False,
                }

            def resolve_path(self, dotted):
                return self.workspace

            def get(self, dotted, default=None):
                return 10 if dotted == "ai.timeout_seconds" else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                calls["communicate_input"] = input
                return '{"type":"result","result":"OK","session_id":"s1"}\n', ""

        def fake_popen(command, **kwargs):
            calls["command"] = command
            calls["kwargs"] = kwargs
            return FakeProcess()

        with TemporaryDirectory() as tmp, patch("services.ai_runner.subprocess.Popen", fake_popen):
            result = AIRunner(FakeConfig(Path(tmp))).run({"user_input": "hi"}, "m1", tool="codebuddy")

        self.assertTrue(result.ok)
        self.assertIsNone(calls["kwargs"]["stdin"])
        self.assertIsNone(calls["communicate_input"])
        self.assertEqual(calls["command"][:5], ["codebuddy", "--print", "--model", "deepseek-v4-flash", "--output-format"])
        self.assertIn('"user_input": "hi"', calls["command"][-1])


if __name__ == "__main__":
    unittest.main()
