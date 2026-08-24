import tempfile
import unittest
import zipfile
from pathlib import Path

from app.models import CaseState, KnowledgeItem, ProposedDocument
from app.services.brief_docx import write_brief_docx
from app.services.brief_flow import collect_brief_sources
from app.services.citations import excerpt_for_cite, extract_article_ref, origin_url, pages_estimate
from app.services.knowledge_flow import _fragments_from_item


class TestArticleRef(unittest.TestCase):
    def test_article_line(self):
        text = "Статья 625. Договор аренды\nАрендодатель обязуется предоставить имущество."
        self.assertIn("625", extract_article_ref(text) or "")

    def test_st_abbrev(self):
        self.assertEqual(extract_article_ref("Ст. 12. Валютные операции"), "Ст. 12. Валютные операции")

    def test_punkt_fallback(self):
        self.assertEqual(extract_article_ref("согласно пункт 3.2 инструкции"), "пункт 3.2")

    def test_none_when_empty(self):
        self.assertIsNone(extract_article_ref("просто абзац без номера"))


class TestExcerpt(unittest.TestCase):
    def test_trims_and_ellipsis(self):
        blob = "слово " * 200
        out = excerpt_for_cite(blob, max_chars=40)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 40)


class TestOriginUrl(unittest.TestCase):
    def test_matches_origin_document_id(self):
        doc = ProposedDocument(
            id="abc123abc123",
            title="ГК РБ",
            doc_type="кодекс",
            why_needed="x",
            found_url="https://pravo.by/document/?guid=3871&p0=hk9800218",
        )
        item = KnowledgeItem(
            title="ГК РБ",
            filename="gk.txt",
            local_path="/tmp/gk.txt",
            origin_document_id="abc123abc123",
        )
        state = CaseState(case_id="c1", inspection_name="Аренда", documents=[doc], knowledge=[item])
        self.assertIn("pravo.by", origin_url(state, item) or "")


class TestPages(unittest.TestCase):
    def test_six_pages(self):
        text = "а" * 11000
        self.assertGreaterEqual(pages_estimate(text, 1800), 6)


class TestBriefDocx(unittest.TestCase):
    def test_writes_hyperlinks_and_bookmarks(self):
        sources = [
            {
                "n": 1,
                "title": "Гражданский кодекс Республики Беларусь",
                "article": "Статья 625. Договор аренды",
                "excerpt": "По договору аренды арендодатель обязуется предоставить имущество.",
                "url": "https://pravo.by/document/?guid=3871&p0=hk9800218",
                "filename": "gk.txt",
            }
        ]
        chapters = [
            {
                "title": "Гражданский кодекс Республики Беларусь",
                "body": "## Ключевые нормы\nДоговор аренды — [1].",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.docx"
            write_brief_docx(
                path,
                inspection_name="Проверка аренды",
                period="2025",
                keywords=["аренда", "НДС"],
                case_id="3a23fb6db4a9",
                overview="Смотрите аренду в [1].",
                chapters=chapters,
                sources=sources,
            )
            self.assertTrue(path.exists())
            from docx import Document

            doc = Document(str(path))
            texts = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("Саммари нормативной базы", texts)
            self.assertIn("Статья 625", texts)
            self.assertIn("[1]", texts)
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8")
                rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            self.assertIn("cite_1", xml)
            self.assertIn("w:hyperlink", xml)
            self.assertIn("pravo.by/document/?guid=3871", rels)
            self.assertIn("TargetMode=\"External\"", rels)


class TestCollectSources(unittest.TestCase):
    def test_numbers_globally_and_keeps_article(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "gk.txt"
            txt.write_text(
                "Статья 625. Договор аренды\n"
                "Арендодатель предоставляет имущество за плату.\n\n"
                "Статья 587. Существенные условия\n"
                "В договоре указывают объект аренды.\n",
                encoding="utf-8",
            )
            item = KnowledgeItem(
                id="item1",
                title="ГК РБ",
                filename="gk.txt",
                local_path=str(txt),
                text_path=str(txt),
                extract_status="ok",
            )
            doc = ProposedDocument(
                title="ГК РБ",
                doc_type="кодекс",
                why_needed="аренда",
                found_url="https://pravo.by/document/?guid=3871&p0=hk9800218",
            )
            state = CaseState(
                case_id="c1",
                inspection_name="Проверка аренды коммерческой недвижимости",
                keywords=["аренда"],
                documents=[doc],
                knowledge=[item],
            )
            sources = collect_brief_sources(state)
            self.assertTrue(sources)
            self.assertEqual(sources[0]["n"], 1)
            self.assertTrue(any(s.get("article") for s in sources))
            self.assertTrue(all(s.get("url") for s in sources))
            frags = _fragments_from_item(state, item)
            self.assertTrue(frags)


class TestDownloadNames(unittest.TestCase):
    def test_summary_docx_uses_inspection_name(self):
        from app.services.brief_flow import brief_download_name

        name = brief_download_name("Проверка аренды коммерческой недвижимости", "abc")
        self.assertTrue(name.endswith("_summary.docx"))
        self.assertIn("аренды", name)
        self.assertNotIn("abc", name)

    def test_archive_zip_uses_npa_suffix(self):
        from app.storage import CaseStore

        name = CaseStore.archive_filename("Проверка аренды коммерческой недвижимости", "abc")
        self.assertTrue(name.endswith("_npa.zip"))
        self.assertNotIn("abc", name)


class TestProgramDocx(unittest.TestCase):
    def test_programma_name_uses_inspection(self):
        from app.services.program_flow import program_download_name

        name = program_download_name("Проверка аренды коммерческой недвижимости", "abc")
        self.assertTrue(name.endswith("_programma.docx"))
        self.assertIn("аренды", name)
        self.assertNotIn("abc", name)

    def test_writes_program_title_and_cites(self):
        from app.services.brief_docx import write_program_docx

        sources = [
            {
                "n": 1,
                "title": "Гражданский кодекс Республики Беларусь",
                "article": "Статья 625. Договор аренды",
                "excerpt": "По договору аренды арендодатель обязуется предоставить имущество.",
                "url": "https://pravo.by/document/?guid=3871&p0=hk9800218",
                "filename": "gk.txt",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "program.docx"
            write_program_docx(
                path,
                inspection_name="Проверка аренды коммерческой недвижимости",
                period="2025",
                keywords=["аренда", "НДС"],
                case_id="3a23fb6db4a9",
                body="## 7. Аудиторские процедуры\n\n### Процедура 1. Договор аренды\n- Критерий: [1].",
                sources=sources,
            )
            self.assertTrue(path.exists())
            from docx import Document

            doc = Document(str(path))
            texts = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("Программа аудиторской проверки", texts)
            self.assertIn("Процедура 1", texts)
            self.assertIn("[1]", texts)


if __name__ == "__main__":
    unittest.main()
