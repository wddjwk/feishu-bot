import unittest

import cards


class CardTests(unittest.TestCase):
    def test_ai_card_uses_tool_emojis_and_thinking_count(self):
        card = cards.build_ai_card("codebuddy", "deepseek-v4-flash", "OK", "abc\n测试")
        self.assertEqual(card["header"]["title"]["content"], "🤝Codebuddy deepseek-v4-flash")
        panel = card["body"]["elements"][2]
        self.assertEqual(panel["header"]["title"]["content"], "分析过程 (6字)")

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
