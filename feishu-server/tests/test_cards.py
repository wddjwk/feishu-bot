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


if __name__ == "__main__":
    unittest.main()
