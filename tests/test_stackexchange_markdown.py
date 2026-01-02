from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestStackExchangeMarkdown(unittest.TestCase):
    def test_renders_question_and_answers_accepted_first(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import render_thread_markdown

        question = {
            "title": "Example question?",
            "link": "https://stackoverflow.com/questions/1/example",
            "score": 10,
            "creation_date": 1700000000,
            "owner": {"link": "https://stackoverflow.com/users/1/u", "display_name": "asker"},
            "body_markdown": "Question body &lt;b&gt;bold&lt;/b&gt;\n\n```py\nprint('hi')\n```",
        }

        answers = [
            {
                "answer_id": 2,
                "score": 5,
                "is_accepted": False,
                "creation_date": 1700000002,
                "owner": {"display_name": "a2"},
                "body_markdown": "Answer 2 body",
            },
            {
                "answer_id": 1,
                "score": 1,
                "is_accepted": True,
                "creation_date": 1700000001,
                "owner": {"display_name": "a1"},
                "body_markdown": "Accepted answer body",
            },
        ]

        md = render_thread_markdown(question=question, answers=answers)

        self.assertIn("# Question", md)
        self.assertIn("Question: Example question?", md)
        self.assertIn("Link: https://stackoverflow.com/questions/1/example", md)
        self.assertIn("Score: 10", md)

        # HTML entity decoded
        self.assertIn("Question body <b>bold</b>", md)

        # Answers header and accepted-first ordering
        self.assertIn("# Answers", md)
        first_answer_idx = md.find("## Answer 1")
        self.assertNotEqual(first_answer_idx, -1)
        accepted_idx = md.find("Accepted Solution")
        non_accepted_idx = md.find("Answer 2 body")
        self.assertNotEqual(accepted_idx, -1)
        self.assertNotEqual(non_accepted_idx, -1)
        self.assertLess(accepted_idx, non_accepted_idx)

    def test_falls_back_to_html_body_when_body_markdown_missing(self) -> None:
        from kindly_web_search_mcp_server.content.stackexchange import render_thread_markdown

        question = {
            "title": "Q",
            "link": "https://stackoverflow.com/questions/1/q",
            "score": 0,
            "creation_date": 1700000000,
            "owner": {"link": "x", "display_name": "asker"},
            # body_markdown intentionally missing:
            "body": "<p>Hello <b>world</b></p>",
        }
        answers = [
            {
                "answer_id": 1,
                "score": 0,
                "is_accepted": True,
                "creation_date": 1700000001,
                "owner": {"display_name": "a1"},
                # body_markdown intentionally missing:
                "body": "<p>Answer</p>",
            }
        ]

        md = render_thread_markdown(question=question, answers=answers)
        # We don't assert exact conversion output (depends on markdownify availability),
        # but it must not be empty.
        self.assertIn("# Question", md)
        self.assertIn("Hello", md)
        self.assertIn("# Answers", md)
        self.assertIn("Answer", md)


if __name__ == "__main__":
    unittest.main()
