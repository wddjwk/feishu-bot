import unittest

import cards


class CardTests(unittest.TestCase):
    def setUp(self):
        cards.configure_card_width("half")
        cards.configure_tool_icons({"codebuddy": "🐇"})
        self.addCleanup(cards.configure_card_width, "half")
        self.addCleanup(cards.configure_tool_icons, {})

    def test_ai_card_uses_tool_emojis_and_thinking_count(self):
        card = cards.build_ai_card("codebuddy", "deepseek-v4-flash", "OK", "abc\n测试")
        self.assertEqual(card["header"]["title"]["content"], "🐇Codebuddy deepseek-v4-flash")
        self.assertNotIn("width_mode", card["config"])
        panel = card["body"]["elements"][2]
        self.assertEqual(panel["header"]["title"]["content"], "分析过程 (6字)")

    def test_full_width_sets_feishu_fill_mode(self):
        cards.configure_card_width("full")

        card = cards.build_generic_card("宽卡片", "内容")

        self.assertEqual(card["config"]["width_mode"], "fill")

    def test_rejects_unknown_card_width(self):
        with self.assertRaises(ValueError):
            cards.configure_card_width("compact")

    def test_markdown_h1_h2_are_downgraded(self):
        self.assertEqual(cards.normalize_markdown("# Title\n## Sub\n### Keep"), "**Title**\n**Sub**\n### Keep")

    def test_long_markdown_is_split_before_truncation(self):
        card = cards.build_generic_card("Long", "x" * (cards.MARKDOWN_CHUNK_CHARS + 10))
        elements = card["body"]["elements"]
        self.assertEqual(len(elements), 2)
        self.assertEqual(len(elements[0]["content"]), cards.MARKDOWN_CHUNK_CHARS)

    def test_timer_list_card_uses_structured_entries(self):
        card = cards.build_timer_list_card(
            [
                {
                    "id": "daily",
                    "name": "每日任务",
                    "cron": "0 9 * * *",
                    "type": "agent",
                    "enabled": True,
                    "run_count": 2,
                }
            ]
        )
        content = card["body"]["elements"][0]["content"]
        self.assertEqual(card["header"]["title"]["content"], "定时任务列表")
        self.assertIn("ID：`daily`", content)


class UsageSuffixTests(unittest.TestCase):
    def setUp(self):
        cards.configure_tool_icons({"codebuddy": "🐇"})
        self.addCleanup(cards.configure_tool_icons, {})

    def test_format_count_units(self):
        self.assertEqual(cards._format_count(None), "0")
        self.assertEqual(cards._format_count(0), "0")
        self.assertEqual(cards._format_count(500), "500")
        self.assertEqual(cards._format_count(999), "999")
        self.assertEqual(cards._format_count(1000), "1.0K")
        self.assertEqual(cards._format_count(1500), "1.5K")
        self.assertEqual(cards._format_count(9999), "10K")
        self.assertEqual(cards._format_count(18928), "19K")
        self.assertEqual(cards._format_count(100_000), "100K")
        self.assertEqual(cards._format_count(999_999), "1000K")
        self.assertEqual(cards._format_count(1_000_000), "1.0M")
        self.assertEqual(cards._format_count(1_500_000), "1.5M")
        self.assertEqual(cards._format_count(10_000_000), "10M")

    def test_format_usage_suffix_empty_cases(self):
        self.assertEqual(cards._format_usage_suffix(None), "")
        from services.ai_runner import TokenUsage
        self.assertEqual(cards._format_usage_suffix(TokenUsage()), "")
        self.assertEqual(cards._format_usage_suffix(TokenUsage(0, 0, 0)), "")

    def test_format_usage_suffix_formats(self):
        from services.ai_runner import TokenUsage
        self.assertEqual(cards._format_usage_suffix(TokenUsage(500, 0, 200)), "500↑/0/200↓")
        self.assertEqual(cards._format_usage_suffix(TokenUsage(18928, 0, 27)), "19K↑/0/27↓")
        self.assertEqual(cards._format_usage_suffix(TokenUsage(27065, 18513, 46)), "27K↑/19K/46↓")

    def test_build_ai_card_with_usage(self):
        from services.ai_runner import TokenUsage
        card = cards.build_ai_card(
            "codebuddy", "deepseek-v4-flash", "OK", "",
            usage=TokenUsage(18928, 0, 27),
        )
        self.assertEqual(
            card["header"]["title"]["content"],
            "🐇Codebuddy deepseek-v4-flash | 19K↑/0/27↓",
        )

    def test_build_ai_card_without_usage_backward_compat(self):
        card = cards.build_ai_card("codebuddy", "deepseek-v4-flash", "OK")
        self.assertEqual(card["header"]["title"]["content"], "🐇Codebuddy deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
