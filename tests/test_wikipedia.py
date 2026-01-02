from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestWikipedia(unittest.TestCase):
    def test_parse_wikipedia_url_wiki_path(self) -> None:
        from kindly_web_search_mcp_server.content.wikipedia import parse_wikipedia_url

        target = parse_wikipedia_url("https://en.wikipedia.org/wiki/Apple_Inc.")
        self.assertEqual(target.host, "en.wikipedia.org")
        self.assertEqual(target.title, "Apple_Inc.")
        self.assertTrue(target.api_base_url.endswith("/w/api.php"))

    def test_parse_wikipedia_url_index_php(self) -> None:
        from kindly_web_search_mcp_server.content.wikipedia import parse_wikipedia_url

        target = parse_wikipedia_url("https://en.wikipedia.org/w/index.php?title=Pet_door")
        self.assertEqual(target.title, "Pet_door")

    def test_mobile_host_normalization(self) -> None:
        from kindly_web_search_mcp_server.content.wikipedia import parse_wikipedia_url

        target = parse_wikipedia_url("https://en.m.wikipedia.org/wiki/Python_(programming_language)")
        self.assertEqual(target.host, "en.wikipedia.org")

    def test_render_truncation_marker(self) -> None:
        from kindly_web_search_mcp_server.content.wikipedia import render_wikipedia_markdown

        md = render_wikipedia_markdown(
            title="T",
            canonical_url="https://en.wikipedia.org/wiki/T",
            host="en.wikipedia.org",
            body_markdown="Hello",
            truncated=True,
        )
        self.assertIn("_Content truncated.", md)


if __name__ == "__main__":
    unittest.main()
