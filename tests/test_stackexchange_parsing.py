from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestStackExchangeUrlParsing(unittest.TestCase):
    def test_parse_stackoverflow_question_url(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url(
            "https://stackoverflow.com/questions/11227809/why-is-processing-a-sorted-array-faster"
        )
        self.assertEqual(target.site, "stackoverflow")
        self.assertEqual(target.question_id, 11227809)
        self.assertIsNone(target.answer_id)

    def test_parse_stackexchange_subdomain_question_url(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url("https://math.stackexchange.com/questions/123/foo")
        self.assertEqual(target.site, "math")
        self.assertEqual(target.question_id, 123)
        self.assertIsNone(target.answer_id)

    def test_parse_superuser_question_url(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url("https://superuser.com/questions/123/foo")
        self.assertEqual(target.site, "superuser")
        self.assertEqual(target.question_id, 123)
        self.assertIsNone(target.answer_id)

    def test_parse_meta_stackoverflow_question_url(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url("https://meta.stackoverflow.com/questions/123/foo")
        self.assertEqual(target.site, "meta.stackoverflow")
        self.assertEqual(target.question_id, 123)
        self.assertIsNone(target.answer_id)

    def test_parse_meta_stackexchange_question_url(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url("https://meta.stackexchange.com/questions/123/foo")
        self.assertEqual(target.site, "meta")
        self.assertEqual(target.question_id, 123)
        self.assertIsNone(target.answer_id)

    def test_parse_answer_shortlink(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import parse_stackexchange_url

        target = parse_stackexchange_url("https://stackoverflow.com/a/87654321")
        self.assertEqual(target.site, "stackoverflow")
        self.assertIsNone(target.question_id)
        self.assertEqual(target.answer_id, 87654321)


if __name__ == "__main__":
    unittest.main()
