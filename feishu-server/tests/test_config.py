import unittest

from config import Config


class ConfigTests(unittest.TestCase):
    def test_tool_launch_and_model_configuration_are_separate(self):
        config = Config()
        expected_transports = {
            "qoder": "stdin",
            "claude": "stdin",
            "copilot": "stdin",
            "codebuddy": "argument",
        }

        self.assertEqual(config.current_tool(), "codebuddy")
        for tool, transport in expected_transports.items():
            with self.subTest(tool=tool):
                launch = config.tool_config(tool)
                model = config.model_config(tool)
                self.assertIn("base_args", launch)
                self.assertNotIn("default_model", launch)
                self.assertIn("default_model", model)
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

        self.assertEqual(config.set_model(None, "max"), "deepseek-v4")
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
        self.assertEqual(options["aliases"]["default"], "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
