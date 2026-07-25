import json
import tempfile
import unittest
from pathlib import Path

from config import Config


class ConfigTests(unittest.TestCase):
    def test_tool_launch_and_model_configuration_are_separate(self):
        config = Config()
        expected_transports = {
            "qoder": "stdin",
            "claude": "stdin",
            "copilot": "stdin",
            "codebuddy": "argument",
            "pi": "stdin",
        }

        self.assertEqual(config.current_tool(), "pi")
        for tool, transport in expected_transports.items():
            with self.subTest(tool=tool):
                launch = config.tool_config(tool)
                model = config.model_config(tool)
                self.assertIn("base_args", launch)
                self.assertNotIn("default_model", launch)
                self.assertIn("default_model", model)
                self.assertNotIn("base_args", model)
                self.assertEqual(launch["prompt_transport"], transport)

    def test_cli_switch_resets_current_model_to_selected_cli_default(self):
        config = Config()
        config.set_model("codebuddy", "custom-model")

        selected = config.set_tool("claude")

        self.assertEqual(selected, config.default_model("claude"))
        self.assertEqual(config.current_tool(), "claude")
        self.assertEqual(config.resolve_model(), config.default_model("claude"))

    def test_model_switch_accepts_alias_or_arbitrary_model_name(self):
        config = Config()
        config.set_tool("claude")

        self.assertEqual(config.set_model(None, "max"), "claude-opus-4.8")
        self.assertEqual(config.set_model(None, "future-model"), "future-model")
        self.assertEqual(config.resolve_model(), "future-model")

    def test_reload_keeps_runtime_cli_and_model_overrides(self):
        config = Config()
        config.set_tool("copilot")
        config.set_model(None, "custom-copilot")

        config.reload()

        self.assertEqual(config.current_tool(), "copilot")
        self.assertEqual(config.resolve_model(), "custom-copilot")

    def test_model_options_include_expandable_models_and_aliases(self):
        config = Config()
        options = config.model_options("codebuddy")

        self.assertIn("deepseek-v4-pro", options["model_list"])
        self.assertEqual(options["default_model"], "deepseek-v4-pro")
        self.assertEqual(options["aliases"]["max"], "deepseek-v4-pro")

    def test_tool_icons_returns_an_icon_per_configured_tool(self):
        config = Config()
        icons = config.tool_icons()

        self.assertEqual(set(icons), set(config.tools()))
        self.assertTrue(all(isinstance(value, str) and value for value in icons.values()))

    def test_user_config_deep_merges_over_base(self):
        base_config = {
            "ai": {
                "default_tool": "claude",
                "tools": {
                    "claude": {
                        "command": "claude",
                        "base_args": ["--print"],
                        "prompt_transport": "stdin",
                        "output_parser": "claude-stream-json",
                        "default_model": "model-a",
                        "model_list": ["model-a", "model-b"],
                        "aliases": {"max": "model-b"},
                        "env": {"BASE_KEY": "base"},
                    }
                },
            }
        }
        user_config = {
            "ai": {
                "tools": {
                    "claude": {
                        "default_model": "model-b",
                        "model_list": ["model-c"],
                        "env": {"USER_KEY": "user"},
                    }
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            user = Path(tmp) / "user.json"
            base.write_text(json.dumps(base_config), encoding="utf-8")
            user.write_text(json.dumps(user_config), encoding="utf-8")

            config = Config(path=base, user_path=user)

        launch = config.tool_config("claude")
        model = config.model_config("claude")
        self.assertEqual(config.default_model("claude"), "model-b")
        self.assertEqual(model["model_list"], ["model-c"])
        self.assertEqual(config.model_environment("claude"), {"BASE_KEY": "base", "USER_KEY": "user"})
        self.assertEqual(launch["command"], "claude")
        self.assertEqual(launch["base_args"], ["--print"])

    def test_user_config_is_optional(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            base.write_text(json.dumps({
                "ai": {
                    "default_tool": "claude",
                    "tools": {
                        "claude": {
                            "command": "claude",
                            "base_args": [],
                            "prompt_transport": "stdin",
                            "output_parser": "claude-stream-json",
                            "default_model": "model-a",
                            "model_list": ["model-a"],
                            "aliases": {},
                            "env": {},
                        }
                    },
                }
            }), encoding="utf-8")

            config = Config(path=base, user_path=Path(tmp) / "missing.json")

        self.assertEqual(config.default_model("claude"), "model-a")

    def test_config_json_supports_line_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            base.write_text(
                '{\n'
                '  "ai": {  // default tool\n'
                '    "default_tool": "claude",\n'
                '    "tools": {\n'
                '      "claude": {\n'
                '        "command": "claude",\n'
                '        "base_args": [],\n'
                '        "prompt_transport": "stdin",\n'
                '        "output_parser": "claude-stream-json",\n'
                '        "default_model": "https://example.com/model",  // url with //\n'
                '        "model_list": ["https://example.com/model"],\n'
                '        "aliases": {},\n'
                '        "env": {}\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )

            config = Config(path=base)

        self.assertEqual(config.default_model("claude"), "https://example.com/model")

    def test_config_json_supports_block_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            base.write_text(
                '{\n'
                '  /* config */\n'
                '  "ai": {\n'
                '    "default_tool": "claude", /* default */\n'
                '    "tools": {\n'
                '      "claude": {\n'
                '        "command": "claude",\n'
                '        "base_args": [],\n'
                '        "prompt_transport": "stdin",\n'
                '        "output_parser": "claude-stream-json",\n'
                '        "default_model": "m",\n'
                '        "model_list": ["m"],\n'
                '        "aliases": {},\n'
                '        "env": {}\n'
                '      }\n'
                '    }\n'
                '  }\n'
                '}\n',
                encoding="utf-8",
            )

            config = Config(path=base)

        self.assertEqual(config.default_model("claude"), "m")

    def test_config_json_supports_trailing_commas(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base.json"
            base.write_text(
                '{\n'
                '  "ai": {\n'
                '    "default_tool": "claude",\n'
                '    "tools": {\n'
                '      "claude": {\n'
                '        "command": "claude",\n'
                '        "base_args": [],\n'
                '        "prompt_transport": "stdin",\n'
                '        "output_parser": "claude-stream-json",\n'
                '        "default_model": "m",\n'
                '        "model_list": ["m",],\n'
                '        "aliases": {"max": "m",},\n'
                '        "env": {},\n'
                '      },\n'
                '    },\n'
                '  },\n'
                '}\n',
                encoding="utf-8",
            )

            config = Config(path=base)

        self.assertEqual(config.default_model("claude"), "m")


if __name__ == "__main__":
    unittest.main()
