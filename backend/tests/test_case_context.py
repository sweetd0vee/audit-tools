import tempfile
import unittest
from pathlib import Path

from app.models import CaseState, KnowledgeItem, ProposedDocument
from app.services.case_context import (
    append_npa_sources_markdown,
    document_catalog,
    existing_cards,
    format_npa_sources,
    optional_block,
)
from app.services.document_artifact import ArtifactSpec, artifact_stale


class TestCaseContext(unittest.TestCase):
    def test_catalog_skips_unselected_and_includes_uploads(self):
        state = CaseState(
            case_id="abc123abc123",
            inspection_name="Проверка аренды",
            documents=[
                ProposedDocument(
                    id="d1",
                    title="ГК РБ",
                    doc_type="кодекс",
                    why_needed="договор",
                    selected=True,
                    download_status="ok",
                ),
                ProposedDocument(
                    id="d2",
                    title="Черновик",
                    doc_type="иное",
                    why_needed="",
                    selected=False,
                    download_status="skipped",
                ),
            ],
            knowledge=[
                KnowledgeItem(
                    title="Политика банка",
                    filename="policy.txt",
                    local_path="policy.txt",
                    source="uploaded",
                )
            ],
        )
        catalog = document_catalog(state)
        self.assertIn("ГК РБ", catalog)
        self.assertIn("скачан", catalog)
        self.assertNotIn("Черновик", catalog)
        self.assertIn("Политика банка", catalog)

    def test_existing_cards_truncates(self):
        state = CaseState(
            case_id="abc123abc123",
            inspection_name="Проверка",
            knowledge=[
                KnowledgeItem(
                    title="НК РБ",
                    filename="nk.txt",
                    local_path="nk.txt",
                    summary_status="ok",
                    summary="x" * 50,
                )
            ],
        )
        cards = existing_cards(state, limit=10)
        self.assertTrue(cards.startswith("# НК РБ"))
        self.assertIn("…", cards)

    def test_format_sources_empty(self):
        self.assertEqual(format_npa_sources([]), "Фрагментов НПА нет.")

    def test_optional_block(self):
        self.assertEqual(
            optional_block("Метка", "  текст  ", "пусто"),
            "Метка:\nтекст\n",
        )
        self.assertEqual(optional_block("Метка", "  ", "пусто"), "пусто\n")

    def test_append_npa_sources_markdown(self):
        with_heading: list[str] = []
        append_npa_sources_markdown(with_heading, [], heading_if_empty=True)
        self.assertEqual(with_heading, ["## Источники: статьи и фрагменты"])

        skipped: list[str] = []
        append_npa_sources_markdown(skipped, [])
        self.assertEqual(skipped, [])

        lines: list[str] = []
        append_npa_sources_markdown(
            lines,
            [
                {
                    "n": 1,
                    "title": "ГК РБ",
                    "article": "Статья 1",
                    "url": "https://pravo.by/a",
                    "excerpt": "текст",
                }
            ],
        )
        self.assertIn("## Источники: статьи и фрагменты", lines)
        self.assertIn("### [1] ГК РБ — Статья 1", lines)
        self.assertIn("https://pravo.by/a", lines)
        self.assertIn("> текст", lines)


class TestArtifactStale(unittest.TestCase):
    def test_missing_file_is_stale(self):
        spec = ArtifactSpec(
            meta_key="brief",
            directory="summaries",
            file_prefix="sammari",
            md_name="brief.md",
            sources_name="brief_sources.json",
            download_suffix="summary",
            docx_endpoint="/x",
            md_endpoint="/y",
            docx_glob="sammari_*.docx",
        )
        state = CaseState(case_id="abc123abc123", inspection_name="Проверка")
        self.assertTrue(artifact_stale(state, spec, schema=4, check_items=True))

    def test_existing_file_and_matching_meta(self):
        spec = ArtifactSpec(
            meta_key="brief",
            directory="summaries",
            file_prefix="sammari",
            md_name="brief.md",
            sources_name="brief_sources.json",
            download_suffix="summary",
            docx_endpoint="/x",
            md_endpoint="/y",
            docx_glob="sammari_*.docx",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.docx"
            path.write_bytes(b"docx")
            state = CaseState(
                case_id="abc123abc123",
                inspection_name="Проверка",
                meta={"brief": {"docx_path": str(path), "schema": 4, "items": 0}},
            )
            self.assertFalse(artifact_stale(state, spec, schema=4, check_items=True))


if __name__ == "__main__":
    unittest.main()
