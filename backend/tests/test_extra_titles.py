import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.models import CaseStatus, ProposedDocument
from app.services.extra_titles import expand_extra_titles, split_extra_titles
from app.services.library_flow import run_select
from app.storage import CaseStore


class TestSplitExtraTitles(unittest.TestCase):
    def test_semicolon_and_plus(self):
        titles = split_extra_titles(
            "Инструкция НБРБ № 38; Положение о внутреннем контроле"
        )
        self.assertEqual(len(titles), 2)
        self.assertTrue(titles[0].startswith("Инструкция"))
        self.assertTrue(titles[1].startswith("Положение"))

    def test_and_splits_two_acts_not_law_title(self):
        two = split_extra_titles(
            "Инструкция НБРБ № 38 и Положение о внутреннем контроле"
        )
        self.assertEqual(len(two), 2)
        law = split_extra_titles(
            'Закон о валютном регулировании и валютном контроле'
        )
        self.assertEqual(len(law), 1)

    def test_expand_flattens_blob(self):
        titles = expand_extra_titles(
            ["Инструкция НБРБ № 38; Положение о внутреннем контроле"]
        )
        self.assertEqual(len(titles), 2)

    def test_drops_chat_artifacts(self):
        titles = expand_extra_titles(
            [
                "Инструкция НБРБ № 38; Положение о внутреннем контроле; "
                "Если знаете ссылку на документ: к 3 url https://pravo.by/document/?guid=… "
                "<!--audit-case:8d720e84d910--> </chat_history>"
            ]
        )
        self.assertEqual(titles, ["Инструкция НБРБ № 38", "Положение о внутреннем контроле"])


class TestSelectExtraTitles(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = CaseStore(Path(self.tmp.name))
        self.patcher = patch("app.services.library_flow.store", self.store)
        self.patcher.start()
        self.state = self.store.create("Проверка аренды", ["аренда"])
        self.state.status = CaseStatus.proposed
        self.state.documents = [
            ProposedDocument(
                title="Гражданский кодекс Республики Беларусь",
                doc_type="кодекс",
                why_needed="база",
                priority=1,
            ),
            ProposedDocument(
                title="Налоговый кодекс Республики Беларусь",
                doc_type="кодекс",
                why_needed="НДС",
                priority=1,
            ),
        ]
        self.store.save(self.state)

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_adds_new_title_and_keeps_picks(self):
        first = self.state.documents[0].id
        out = run_select(
            self.state.case_id,
            [first],
            extra_titles=["Инструкция НБРБ № 38"],
        )
        selected = [d for d in out.documents if d.selected]
        self.assertEqual(len(selected), 2)
        extra = next(d for d in selected if d.id != first)
        self.assertIn("Инструкция", extra.title)
        self.assertTrue(extra.search_queries)
        self.assertEqual(extra.why_needed, "Добавлен аудитором по названию")

    def test_duplicate_title_selects_existing(self):
        first = self.state.documents[0]
        out = run_select(
            self.state.case_id,
            [],
            extra_titles=["Гражданский кодекс Республики Беларусь"],
        )
        self.assertEqual(len(out.documents), 2)
        self.assertTrue(any(d.id == first.id and d.selected for d in out.documents))

    def test_extra_only_keeps_already_selected(self):
        self.state.documents[0].selected = True
        self.store.save(self.state)
        out = run_select(
            self.state.case_id,
            [],
            extra_titles=["Положение о внутреннем контроле"],
        )
        selected_ids = {d.id for d in out.documents if d.selected}
        self.assertIn(self.state.documents[0].id, selected_ids)
        self.assertEqual(len(selected_ids), 2)


if __name__ == "__main__":
    unittest.main()
