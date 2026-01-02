from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TestArxivParsing(unittest.TestCase):
    def test_parse_arxiv_url_new_id(self) -> None:
        from kindly_web_search_mcp_server.content.arxiv import parse_arxiv_url

        self.assertEqual(parse_arxiv_url("https://arxiv.org/abs/2205.01491"), "2205.01491")
        self.assertEqual(parse_arxiv_url("https://arxiv.org/pdf/2205.01491"), "2205.01491")
        self.assertEqual(parse_arxiv_url("https://arxiv.org/pdf/2205.01491.pdf"), "2205.01491")
        self.assertEqual(parse_arxiv_url("https://arxiv.org/abs/2205.01491v2"), "2205.01491v2")
        self.assertEqual(parse_arxiv_url("https://arxiv.org/pdf/2205.01491v2.pdf?download=1"), "2205.01491v2")

    def test_parse_arxiv_url_legacy_id(self) -> None:
        from kindly_web_search_mcp_server.content.arxiv import parse_arxiv_url

        self.assertEqual(parse_arxiv_url("https://arxiv.org/abs/hep-th/9901001"), "hep-th/9901001")
        self.assertEqual(parse_arxiv_url("https://arxiv.org/pdf/hep-th/9901001v1.pdf"), "hep-th/9901001v1")

    def test_parse_arxiv_url_rejects_non_arxiv(self) -> None:
        from kindly_web_search_mcp_server.content.arxiv import ArxivError, parse_arxiv_url

        with self.assertRaises(ArxivError):
            parse_arxiv_url("https://example.com/abs/2205.01491")


class TestArxivAtomParsing(unittest.TestCase):
    def test_parse_atom_xml_extracts_metadata(self) -> None:
        from kindly_web_search_mcp_server.content.arxiv import _parse_arxiv_atom_xml

        xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2205.01491v1</id>
    <updated>2022-05-03T00:00:00Z</updated>
    <published>2022-05-01T00:00:00Z</published>
    <title>  Test Title  </title>
    <summary>
      This is an abstract.
    </summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <link rel="alternate" type="text/html" href="http://arxiv.org/abs/2205.01491v1"/>
    <link title="pdf" rel="related" type="application/pdf" href="http://arxiv.org/pdf/2205.01491v1"/>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.AI"/>
  </entry>
</feed>
"""
        meta = _parse_arxiv_atom_xml(xml, arxiv_id="2205.01491v1")
        self.assertEqual(meta.arxiv_id, "2205.01491v1")
        self.assertEqual(meta.title, "Test Title")
        self.assertIn("Alice", meta.authors)
        self.assertIn("Bob", meta.authors)
        self.assertEqual(meta.primary_category, "cs.CL")
        self.assertIn("cs.CL", meta.categories)
        self.assertIn("cs.AI", meta.categories)
        self.assertEqual(meta.pdf_url, "http://arxiv.org/pdf/2205.01491v1")
        self.assertEqual(meta.abs_url, "https://arxiv.org/abs/2205.01491v1")


class TestPdfToMarkdown(unittest.TestCase):
    def test_pdf_bytes_to_markdown_contains_text(self) -> None:
        # Prefer modern `pymupdf`, but allow `fitz` for older installs.
        try:
            import pymupdf  # type: ignore
        except Exception:
            pymupdf = None  # type: ignore
        if pymupdf is None:
            try:
                import fitz as pymupdf  # type: ignore
            except Exception:
                self.skipTest("PyMuPDF not installed in this environment")

        from kindly_web_search_mcp_server.content.arxiv import _pdf_bytes_to_markdown_best_effort

        doc = pymupdf.open()
        try:
            page = doc.new_page()
            page.insert_text((72, 72), "Hello arXiv")
            pdf_bytes = doc.tobytes()
        finally:
            doc.close()

        result = _pdf_bytes_to_markdown_best_effort(pdf_bytes, max_pages=1)
        self.assertIn("Hello", result.markdown)


if __name__ == "__main__":
    unittest.main()
