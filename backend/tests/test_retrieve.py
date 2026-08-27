from __future__ import annotations

import unittest

from app.services.chunker import chunk_text, normalize_npa_text
from app.services.knowledge_retrieve import (
    article_nums_in_query,
    article_query_variants,
    gate_ask_evidence,
)


class TestNormalizeNpa(unittest.TestCase):
    def test_article_becomes_markdown_heading(self):
        text = "Статья 625. Договор аренды\nАрендодатель передаёт имущество.\n"
        out = normalize_npa_text(text)
        self.assertIn("## Статья 625. Договор аренды", out)
        again = normalize_npa_text(out)
        self.assertEqual(out, again)

    def test_chunk_splits_on_articles(self):
        text = (
            "Статья 1. Общие положения\n"
            + ("преамбула кодекса без аренды " * 40)
            + "\n\nСтатья 625. Договор аренды\nсрок регистрации договора аренды недвижимости.\n"
        )
        parts = chunk_text(text, size=400, overlap=40)
        self.assertTrue(any("625" in p and "аренды" in p.lower() for p in parts))
        self.assertGreaterEqual(len(parts), 2)


class TestArticleMentions(unittest.TestCase):
    def test_variants_include_markdown_heading(self):
        variants = article_query_variants("Какой срок в ст. 625 ГК?")
        self.assertIn("Статья 625", variants)
        self.assertIn("## Статья 625", variants)
        self.assertEqual(article_nums_in_query("ст. 625 и статья 12.1"), {"625", "12.1"})

    def test_punkt_variants(self):
        variants = article_query_variants("см. пункт 3.2 инструкции")
        self.assertTrue(any("3.2" in v for v in variants))


class TestGateAskEvidence(unittest.TestCase):
    def test_missing_article_refuses(self):
        evidence = [
            {
                "id": "gk:1",
                "title": "Гражданский кодекс Республики Беларусь",
                "filename": "gk.txt",
                "text": "## Статья 625. Договор аренды\nрегистрация",
                "rerank_score": 0.9,
            }
        ]
        self.assertEqual(
            gate_ask_evidence("статья 999 ГК", evidence, corpus_articles={"625"}),
            [],
        )

    def test_low_rerank_without_heading_drops(self):
        evidence = [
            {
                "id": "noise:0",
                "title": "Инструкция",
                "filename": "i.txt",
                "text": "Канцелярия и общие слова срок договор",
                "rerank_score": 0.05,
            }
        ]
        self.assertEqual(
            gate_ask_evidence("срок лизинга по МСФО", evidence, corpus_articles=set()),
            [],
        )

    def test_heading_match_kept_even_if_rerank_modest(self):
        evidence = [
            {
                "id": "gk:1",
                "title": "Гражданский кодекс Республики Беларусь",
                "filename": "gk.txt",
                "text": "## Статья 625. Договор аренды\nрегистрация",
                "rerank_score": 0.2,
            }
        ]
        kept = gate_ask_evidence("ст. 625 ГК", evidence, corpus_articles={"625"})
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
