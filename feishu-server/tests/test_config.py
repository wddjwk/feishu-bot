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
        for tool, flag in expected.items():
            with self.subTest(tool=tool):
                self.assertIn(flag, config.tool_config(tool)["args"])


if __name__ == "__main__":
    unittest.main()
