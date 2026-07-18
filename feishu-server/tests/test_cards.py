import unittest

import cards


class CardTests(unittest.TestCase):
    def setUp(self):
        cards.configure_card_width("half")
        self.addCleanup(cards.configure_card_width, "half")

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


if __name__ == "__main__":
    unittest.main()
