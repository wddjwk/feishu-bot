import unittest

from display import shape_for_display
from services.ai_runner import AIResult


class FakeConfig:
    def __init__(self, limit):
        self.limit = limit

    def get(self, dotted, default=None):
        if dotted == "options.features.show_max_tool_result_length":
            return self.limit
        return default


def _result(thinking: str) -> AIResult:
    return AIResult(ok=True, tool="claude", model="m", thinking=thinking, result="done")


class ShapeForDisplayTests(unittest.TestCase):
    def test_truncates_long_tool_result_section(self):
        long = "x" * 600
        thinking = f"🛠 Tool bash\ncmd\n\n📎 Tool result\n{long}\n\n🧠 Thinking\nsummary"
        out = shape_for_display(_result(thinking), FakeConfig(500))

        self.assertIn("[truncated]", out.thinking)
        self.assertNotIn(long, out.thinking)
        self.assertIn("🛠 Tool bash\ncmd", out.thinking)
        self.assertIn("🧠 Thinking\nsummary", out.thinking)

    def test_short_tool_result_unchanged(self):
        thinking = "📎 Tool result\nshort output"
        out = shape_for_display(_result(thinking), FakeConfig(500))

        self.assertEqual(out.thinking, thinking)

    def test_no_limit_leaves_thinking_untouched(self):
        thinking = "📎 Tool result\n" + "x" * 1000
        out = shape_for_display(_result(thinking), FakeConfig(0))

        self.assertEqual(out.thinking, thinking)

    def test_tool_result_error_variant_truncated(self):
        long = "y" * 600
        thinking = f"📎 Tool result (error)\n{long}"
        out = shape_for_display(_result(thinking), FakeConfig(50))

        self.assertIn("[truncated]", out.thinking)

    def test_preserves_separator_and_following_sections(self):
        long = "z" * 600
        thinking = f"📎 Tool result\n{long}\n\n🛠 Tool next\nargs"
        out = shape_for_display(_result(thinking), FakeConfig(100))

        self.assertIn("\n\n🛠 Tool next\nargs", out.thinking)
        self.assertIn("[truncated]", out.thinking)

    def test_does_not_touch_result_text(self):
        thinking = "📎 Tool result\n" + "x" * 600
        out = shape_for_display(_result(thinking), FakeConfig(100))

        self.assertEqual(out.result, "done")


if __name__ == "__main__":
    unittest.main()
