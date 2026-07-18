import unittest

from config import Config


class ConfigTests(unittest.TestCase):
    def test_ai_tools_include_permission_bypass_flags(self):
        config = Config()
        expected = {
            "qoder": "--dangerously-skip-permissions",
            "claude": "--dangerously-skip-permissions",
            "copilot": "--allow-all",
            "codebuddy": "--dangerously-skip-permissions",
        }
        expected_transports = {
            "qoder": "stdin",
            "claude": "stdin",
            "copilot": "stdin",
            "codebuddy": "argument",
        }
        expected_parsers = {
            "qoder": "stream_json",
            "claude": "stream_json",
            "copilot": "copilot_json",
            "codebuddy": "stream_json",
        }
        for tool, flag in expected.items():
            with self.subTest(tool=tool):
                self.assertIn(flag, config.tool_config(tool)["base_args"])
                self.assertEqual(config.tool_config(tool)["session_args"], ["--session-id", "{session_id}"])
                self.assertEqual(config.tool_config(tool)["prompt_transport"], expected_transports[tool])
                self.assertEqual(config.tool_config(tool)["output_parser"], expected_parsers[tool])


if __name__ == "__main__":
    unittest.main()
