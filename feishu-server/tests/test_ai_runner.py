import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from services.ai_runner import (
    AIRunner,
    BaseOutputParser,
    ClaudeParser,
    CodeBuddyParser,
    CopilotParser,
    PiParser,
    QoderCliParser,
    get_parser,
    parse_output,
)


class ParseOutputTests(unittest.TestCase):
    def test_claude_result_event(self):
        stdout = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"checking"}]},"session_id":"s1"}',
                '{"type":"result","result":"final answer","usage":{"input_tokens":3,"output_tokens":4}}',
            ]
        )
        result = parse_output("claude-stream-json", stdout)
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
        result = parse_output("claude-stream-json", stdout)
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
        result = parse_output("copilot-json", stdout)
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
        result = parse_output("copilot-json", stdout)
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
        result = parse_output("copilot-json", stdout)
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "OK")
        self.assertEqual(result.session_id, "s1")
        self.assertEqual(result.usage.output_tokens, 4)

    def test_plain_stdout_fallback(self):
        result = parse_output("claude-stream-json", "hello\nworld\n")
        self.assertTrue(result.ok)
        self.assertEqual(result.result, "world")

    def test_json_error_event_is_failure(self):
        stdout = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"text","text":"rate limited"}]}}',
                '{"type":"result","subtype":"error_during_execution","is_error":true,"errors":["rate limited"]}',
            ]
        )
        result = parse_output("claude-stream-json", stdout)
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
                    "base_args": ["--print", "--model", "{model}", "--output-format", "stream-json"],
                    "session_args": ["--session-id", "{session_id}"],
                    "resume_args": ["--resume", "{session_id}"],
                    "prompt_transport": "argument",
                    "output_parser": "claude-stream-json",
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

    def test_session_id_is_passed_for_new_sessions_but_not_resumes(self):
        class FakeConfig:
            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": ["--model", "{model}"],
                    "session_args": ["--session-id", "{session_id}"],
                    "resume_args": ["--resume", "{session_id}"],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

        runner = AIRunner(FakeConfig())
        new_command = runner._build_command("claude", "model-a", "new-session", None)
        resume_command = runner._build_command("claude", "model-a", None, "existing-session")

        self.assertEqual(new_command, ["claude", "--model", "model-a", "--session-id", "new-session"])
        self.assertEqual(resume_command, ["claude", "--model", "model-a", "--resume", "existing-session"])

    def test_requested_session_id_takes_precedence_over_output_metadata(self):
        calls = {}

        class FakeConfig:
            def current_tool(self):
                return "claude"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": [],
                    "session_args": ["--session-id", "{session_id}"],
                    "resume_args": ["--resume", "{session_id}"],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

            def resolve_path(self, _dotted):
                return Path(".")

            def get(self, _dotted, default=None):
                return 10 if default is None else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                calls["command_input"] = input
                return '{"type":"result","result":"OK","session_id":"unexpected-session"}\n', ""

        with patch("services.ai_runner.subprocess.Popen", return_value=FakeProcess()):
            result = AIRunner(FakeConfig()).run("hello", "m1", session_id="requested-session")

        self.assertTrue(result.ok)
        self.assertEqual(result.session_id, "requested-session")
        self.assertEqual(calls["command_input"], "hello")

    def test_tool_spec_is_reloaded_for_each_execution(self):
        class MutableConfig:
            def __init__(self) -> None:
                self.args = ["--model", "{model}"]

            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": self.args,
                    "session_args": ["--session-id", "{session_id}"],
                    "resume_args": ["--resume", "{session_id}"],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

        config = MutableConfig()
        runner = AIRunner(config)
        first = runner._build_command("claude", "model-a", "session-a", None)
        config.args = ["--model", "{model}", "--verbose"]
        second = runner._build_command("claude", "model-a", "session-b", None)

        self.assertNotIn("--verbose", first)
        self.assertIn("--verbose", second)

    def test_legacy_tool_without_session_args_uses_cli_reported_session(self):
        class LegacyConfig:
            def current_tool(self):
                return "legacy"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "legacy-cli",
                    "args": ["--model", "{model}"],
                    "stdin": True,
                }

            def resolve_path(self, _dotted):
                return Path(".")

            def get(self, _dotted, default=None):
                return 10 if default is None else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return '{"type":"result","result":"OK","session_id":"native-session"}\n', ""

        with patch("services.ai_runner.subprocess.Popen", return_value=FakeProcess()) as popen:
            runner = AIRunner(LegacyConfig())
            result = runner.run("hello", "m1")

        self.assertFalse(runner.supports_explicit_session("legacy"))
        self.assertEqual(result.session_id, "native-session")
        self.assertNotIn("--session-id", popen.call_args.args[0])

    def test_argument_prompt_limit_prevents_os_argv_failure(self):
        class FakeConfig:
            def current_tool(self):
                return "codebuddy"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "codebuddy",
                    "base_args": ["--print"],
                    "prompt_transport": "argument",
                    "output_parser": "claude-stream-json",
                }

            def get(self, dotted, default=None):
                if dotted == "ai.max_prompt_length_bytes":
                    return 5
                return default

        result = AIRunner(FakeConfig()).run("超过上限", "m1")

        self.assertFalse(result.ok)
        self.assertIn("超过参数输入上限", result.error)

    def test_model_environment_is_injected_into_cli_process(self):
        calls = {}

        class FakeConfig:
            def current_tool(self):
                return "claude"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": [],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

            def model_environment(self, _tool):
                return {"MODEL_ENDPOINT": "https://model.example.test", "MODEL_FLAG": "1"}

            def resolve_path(self, _dotted):
                return Path(".")

            def get(self, _dotted, default=None):
                return 10 if default is None else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return '{"type":"result","result":"OK"}\n', ""

        def fake_popen(_command, **kwargs):
            calls["env"] = kwargs["env"]
            return FakeProcess()

        with patch("services.ai_runner.subprocess.Popen", fake_popen):
            result = AIRunner(FakeConfig()).run("hello", "m1")

        self.assertTrue(result.ok)
        self.assertEqual(calls["env"]["MODEL_ENDPOINT"], "https://model.example.test")
        self.assertEqual(calls["env"]["MODEL_FLAG"], "1")

    def test_logs_parsed_result_on_success(self):
        class FakeConfig:
            def current_tool(self):
                return "claude"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": [],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

            def resolve_path(self, _dotted):
                return Path(".")

            def get(self, _dotted, default=None):
                return 10 if default is None else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return '{"type":"result","result":"简洁回复"}\n', ""

        with patch("services.ai_runner.subprocess.Popen", return_value=FakeProcess()), self.assertLogs(
            "feishu_server", level="INFO"
        ) as logs:
            result = AIRunner(FakeConfig()).run("用户问题", "m1")

        output = "\n".join(logs.output)
        self.assertTrue(result.ok)
        self.assertIn("AI 执行成功", output)
        self.assertIn("AI 回复：", output)
        self.assertIn("简洁回复", output)
        self.assertNotIn('{"type":"result"', output)

    def test_always_runs_from_agent_workspace(self):
        calls = {}

        class FakeConfig:
            def __init__(self, workspace: Path) -> None:
                self.workspace = workspace

            def current_tool(self):
                return "claude"

            def resolve_model(self, _tool):
                return "model-a"

            def tool_config(self, _tool):
                return {
                    "command": "claude",
                    "base_args": [],
                    "prompt_transport": "stdin",
                    "output_parser": "claude-stream-json",
                }

            def resolve_path(self, _dotted):
                return self.workspace

            def get(self, _dotted, default=None):
                return 10 if default is None else default

        class FakeProcess:
            pid = 12345
            returncode = 0

            def communicate(self, input=None, timeout=None):
                return '{"type":"result","result":"OK"}\n', ""

        def fake_popen(_command, **kwargs):
            calls["cwd"] = kwargs["cwd"]
            return FakeProcess()

        with TemporaryDirectory() as tmp, patch("services.ai_runner.subprocess.Popen", fake_popen):
            workspace = Path(tmp) / "agent-workspace"
            result = AIRunner(FakeConfig(workspace)).run("hello", "m1")

        self.assertTrue(result.ok)
        self.assertEqual(calls["cwd"], str(workspace))


class ParserRegistryTests(unittest.TestCase):
    """验证解析器注册表与工厂的自动注册、inherits 跳过机制、继承关系。"""

    def test_only_canonical_names_registered(self):
        self.assertEqual(
            set(BaseOutputParser._registry),
            {"claude-stream-json", "pi-json", "copilot-json"},
        )

    def test_qodercli_and_codebuddy_exist_but_not_registered(self):
        self.assertTrue(issubclass(QoderCliParser, ClaudeParser))
        self.assertTrue(issubclass(CodeBuddyParser, ClaudeParser))
        self.assertEqual(QoderCliParser.inherits, "claude-stream-json")
        self.assertEqual(CodeBuddyParser.inherits, "claude-stream-json")
        self.assertNotIn("qodercli-stream-json", BaseOutputParser._registry)
        self.assertNotIn("codebuddy-stream-json", BaseOutputParser._registry)

    def test_canonical_names_resolve(self):
        self.assertIsInstance(get_parser("claude-stream-json"), ClaudeParser)
        self.assertIsInstance(get_parser("pi-json"), PiParser)
        self.assertIsInstance(get_parser("copilot-json"), CopilotParser)

    def test_unknown_parser_name_raises(self):
        with self.assertRaises(ValueError):
            get_parser("unknown-parser")
        with self.assertRaises(ValueError):
            get_parser("qodercli-stream-json")
        with self.assertRaises(ValueError):
            get_parser("stream_json")

    def test_parse_output_with_canonical_names(self):
        claude_stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n'
            '{"type":"result","result":"hi","session_id":"s"}'
        )
        self.assertEqual(parse_output("claude-stream-json", claude_stdout).result, "hi")

        pi_stdout = (
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"ok"}}\n'
            '{"type":"turn_end","message":{"stopReason":"stop","content":[{"type":"text","text":"ok"}]}}'
        )
        self.assertEqual(parse_output("pi-json", pi_stdout).result, "ok")

        copilot_stdout = '{"type":"content_delta","delta":"ab"}\n{"type":"result","result":"ab"}'
        self.assertEqual(parse_output("copilot-json", copilot_stdout).result, "ab")

    def test_subclasses_reuse_parent_parse(self):
        claude_stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"x"}]}}\n'
            '{"type":"result","result":"x"}'
        )
        self.assertEqual(QoderCliParser().parse(claude_stdout, "").result, "x")
        self.assertEqual(CodeBuddyParser().parse(claude_stdout, "").result, "x")


class InlineThinkingTests(unittest.TestCase):
    """验证模型把 reasoning 内联在正文 text 时，能被抽成 thinking 流。"""

    def test_claude_result_strips_think_tag(self):
        stdout = '{"type":"result","result":"<think>step 1</think>ok"}\n'
        result = parse_output("claude-stream-json", stdout)
        self.assertEqual(result.result, "ok")
        self.assertIn("step 1", result.thinking)

    def test_claude_result_strips_thinking_tag(self):
        stdout = '{"type":"result","result":"<thinking>step 1</thinking>ok"}\n'
        result = parse_output("claude-stream-json", stdout)
        self.assertEqual(result.result, "ok")
        self.assertIn("step 1", result.thinking)

    def test_pi_turn_end_strips_think_tag(self):
        stdout = '{"type":"turn_end","message":{"stopReason":"stop","content":[{"type":"text","text":"<think>step 1</think>ok"}]}}\n'
        result = parse_output("pi-json", stdout)
        self.assertEqual(result.result, "ok")
        self.assertIn("step 1", result.thinking)

    def test_pi_text_delta_strips_think_tag(self):
        stdout = (
            '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","delta":"<think>step 1</think>ok"}}\n'
            '{"type":"turn_end","message":{"stopReason":"stop","content":[]}}\n'
        )
        result = parse_output("pi-json", stdout)
        self.assertEqual(result.result, "ok")
        self.assertIn("step 1", result.thinking)

    def test_claude_assistant_text_strips_inline_thinking(self):
        stdout = (
            '{"type":"assistant","message":{"content":[{"type":"text","text":"<think>reasoning</think>answer"}]}}\n'
            '{"type":"result","result":"ok"}\n'
        )
        result = parse_output("claude-stream-json", stdout)
        self.assertEqual(result.result, "ok")
        self.assertIn("reasoning", result.thinking)


if __name__ == "__main__":
    unittest.main()
