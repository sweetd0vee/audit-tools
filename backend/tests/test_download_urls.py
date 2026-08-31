import unittest

from app.models import ProposedDocument
from app.services.downloader import is_usable_npa_page, usable_url
from app.services.known_sources import lookup_known_url
from app.services.library_flow import download_candidates
from app.services.npa_search import build_search_queries, expand_official_urls, score_url


class TestUsableUrl(unittest.TestCase):
    def test_placeholder_with_dots_is_rejected(self):
        self.assertIsNone(usable_url("https://pravo.by/..."))
        self.assertIsNone(usable_url("https://pravo.by/...`"))
        self.assertIsNone(usable_url("`https://pravo.by/...`"))

    def test_strips_wrapping_punctuation(self):
        url = usable_url("`https://pravo.by/document/?guid=3871&p0=hk9800218`")
        self.assertEqual(url, "https://pravo.by/document/?guid=3871&p0=hk9800218")

    def test_bare_host_rejected(self):
        self.assertIsNone(usable_url("https://pravo.by"))
        self.assertIsNone(usable_url("https://pravo.by/"))

    def test_off_allowlist_rejected(self):
        self.assertIsNone(usable_url("https://example.com/doc.pdf"))

    def test_official_document_ok(self):
        url = "https://pravo.by/document/?guid=12551&p0=H12200136&p1=1"
        self.assertEqual(usable_url(url), url)


class TestKnownSources(unittest.TestCase):
    def test_currency_law_has_official_url(self):
        title = (
            'Закон Республики Беларусь от 09.06.2005 № 97-З '
            '"О валютном регулировании и валютном контроле"'
        )
        url = lookup_known_url(title)
        self.assertIsNotNone(url)
        self.assertIn("H12200136", url)

    def test_nbrb_instruction_is_not_minfin_auditor_page(self):
        title = (
            "Инструкция Национального банка Республики Беларусь "
            'от 18.06.2021 № 180 "Об открытии и использовании счетов в банках"'
        )
        self.assertIsNone(lookup_known_url(title))

    def test_civil_code_still_matches(self):
        self.assertIn("hk9800218", lookup_known_url("Гражданский кодекс Республики Беларусь") or "")

    def test_lease_and_internal_audit_aliases(self):
        self.assertIn(
            "W21833716",
            lookup_known_url("Положение о бухгалтерском учете аренды") or "",
        )
        self.assertIn(
            "P32300138",
            lookup_known_url(
                "Указ Президента Республики Беларусь "
                "«О некоторых вопросах регулирования арендных отношений в сфере недвижимости»"
            )
            or "",
        )
        self.assertIn(
            "B21326759",
            lookup_known_url(
                "Инструкция НБРБ «О порядке проведения внутреннего аудита в банках Республики Беларусь»"
            )
            or "",
        )
        self.assertIn(
            "B21529598",
            lookup_known_url(
                "Инструкция НБРБ «О требованиях к внутреннему контролю за проведением банковских операций»"
            )
            or "",
        )
        self.assertIn(
            "B21428262",
            lookup_known_url(
                "Инструкция НБРБ «О порядке оформления и хранения банковских документов»"
            )
            or "",
        )

    def test_strips_trailing_backtick(self):
        self.assertIn(
            "B21326759",
            lookup_known_url("Положение о внутреннем контроле`") or "",
        )
        self.assertIn(
            "H11300057",
            lookup_known_url("Закон Республики Беларусь О бухгалтерском учете и отчетности") or "",
        )

    def test_property_and_lease_instruction_is_not_lease_accounting(self):
        title = (
            "Инструкция о порядке бухгалтерского учета операций с имуществом и аренды "
            "в банках Республики Беларусь"
        )
        url = lookup_known_url(title) or ""
        self.assertIn("B22340032", url)
        self.assertNotIn("W21833716", url)

    def test_lease_accounting_is_not_property_instruction(self):
        url = lookup_known_url("Положение о бухгалтерском учете аренды") or ""
        self.assertIn("W21833716", url)
        self.assertNotIn("B22340032", url)

    def test_civil_code_url_conflicts_with_instruction_title(self):
        from app.services.known_sources import url_code_conflicts_title

        title = "Инструкция НБРБ О порядке проведения внутреннего аудита в банках"
        self.assertTrue(url_code_conflicts_title("hk9800218", title))
        self.assertFalse(url_code_conflicts_title("hk9800218", "Гражданский кодекс Республики Беларусь"))
        self.assertFalse(url_code_conflicts_title("hk9800218", "ГК РБ"))


class TestDownloadCandidates(unittest.TestCase):
    def test_placeholder_falls_back_to_known_source(self):
        doc = ProposedDocument(
            title='Закон "О валютном регулировании и валютном контроле"',
            doc_type="закон",
            why_needed="x",
            found_url="https://pravo.by/...`",
        )
        candidates = download_candidates(doc)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0][1], "known")
        self.assertIn("H12200136", candidates[0][0])

    def test_valid_manual_url_stays_first(self):
        manual = "https://pravo.by/document/?guid=12551&p0=H12200136&p1=1"
        doc = ProposedDocument(
            title='Закон "О валютном регулировании и валютном контроле"',
            doc_type="закон",
            why_needed="x",
            found_url=manual,
        )
        candidates = download_candidates(doc)
        self.assertEqual(candidates[0], (manual, "manual"))

    def test_news_manual_url_is_skipped_for_known_source(self):
        doc = ProposedDocument(
            title="Положение о бухгалтерском учете аренды",
            doc_type="положение",
            why_needed="x",
            found_url="https://pravo.by/novosti/analitika/2023/december/76364/",
        )
        candidates = download_candidates(doc)
        self.assertTrue(candidates)
        self.assertTrue(all("/novosti/" not in url for url, _src in candidates))
        self.assertIn("W21833716", candidates[0][0])


class TestSearchQueries(unittest.TestCase):
    def test_nbrb_title_searches_nbrb_and_etalonline(self):
        queries = build_search_queries(
            [],
            "Инструкция НБРБ «О порядке проведения внутреннего аудита в банках Республики Беларусь»",
        )
        blob = "\n".join(queries)
        self.assertIn("site:nbrb.by", blob)
        self.assertIn("site:etalonline.by", blob)
        self.assertTrue(any("внутреннего аудита" in q.lower() or "внутреннем аудите" in q.lower() for q in queries))

    def test_does_not_stop_at_pravo_only(self):
        queries = build_search_queries([], "Инструкция НБРБ № 38")
        self.assertGreaterEqual(len(queries), 3)
        self.assertTrue(any("nbrb.by" in q for q in queries))


class TestOfficialUrlExpand(unittest.TestCase):
    def test_card_expands_to_fulltext(self):
        variants = expand_official_urls(
            "https://pravo.by/document/?guid=12551&p0=H11300057"
        )
        self.assertTrue(any("guid=3871" in u for u in variants))
        self.assertTrue(any("etalonline.by" in u for u in variants))
        self.assertTrue(any("webnpa/text" in u for u in variants))

    def test_fulltext_outranks_news(self):
        title = "Положение о бухгалтерском учете аренды"
        news = "https://pravo.by/novosti/analitika/2023/december/76364/"
        doc = "https://pravo.by/document/?guid=3871&p0=W21833716"
        self.assertGreater(score_url(doc, title), score_url(news, title))

    def test_banking_code_not_used_for_unrelated_instruction(self):
        title = "Инструкция НБРБ О порядке проведения внутреннего аудита в банках"
        code = "https://pravo.by/document/?guid=3871&p0=hk0000441"
        other = "https://etalonline.by/document/?regnum=B21428262"
        self.assertLess(score_url(code, title), score_url(other, title))
        self.assertLess(score_url(code, title), -40)

    def test_accounting_law_does_not_outrank_lease_position(self):
        title = "Положение о бухгалтерском учете аренды"
        law = "https://pravo.by/document/?guid=3871&p0=H11300057"
        lease = "https://pravo.by/document/?guid=3871&p0=W21833716"
        self.assertGreater(score_url(lease, title, title), score_url(law, title, title))
        self.assertLess(score_url(law, title, title), 20)

    def test_empty_hit_title_does_not_boost_from_query_title(self):
        title = "Положение о бухгалтерском учете аренды"
        stray = "https://etalonline.by/document/?regnum=hk9800218"
        self.assertLess(score_url(stray, title, ""), 20)

    def test_news_page_is_not_usable_npa(self):
        html = (
            "<html><body>Аренда объектов недвижимости: новеллы в Гражданском кодексе"
            + (" текст" * 200)
            + "</body></html>"
        ).encode("utf-8")
        self.assertFalse(
            is_usable_npa_page(
                "https://pravo.by/novosti/analitika/2023/december/76364/",
                html,
                "text/html",
            )
        )


class TestShouldRedownload(unittest.TestCase):
    def test_official_code_url_is_kept_when_not_in_catalog(self):
        from app.services.library_flow import _should_redownload

        doc = ProposedDocument(
            title="Инструкция НБРБ № 38 Об открытии счетов",
            doc_type="инструкция",
            why_needed="x",
            found_url="https://pravo.by/document/?guid=3871&p0=B22199999",
            download_status="ok",
        )
        self.assertFalse(_should_redownload(doc))

    def test_news_url_is_redownloaded(self):
        from app.services.library_flow import _should_redownload

        doc = ProposedDocument(
            title="Положение о бухгалтерском учете аренды",
            doc_type="положение",
            why_needed="x",
            found_url="https://pravo.by/novosti/analitika/2023/december/76364/",
            download_status="ok",
        )
        self.assertTrue(_should_redownload(doc))

    def test_wrong_catalog_code_is_redownloaded(self):
        from app.services.library_flow import _should_redownload

        doc = ProposedDocument(
            title="Положение о бухгалтерском учете аренды",
            doc_type="положение",
            why_needed="x",
            found_url="https://pravo.by/document/?guid=3871&p0=H11300057",
            download_status="ok",
        )
        self.assertTrue(_should_redownload(doc))


class TestDownloadRejectsWrongAct(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from app.models import CaseStatus
        from app.storage import CaseStore

        self.tmp = TemporaryDirectory()
        self.store = CaseStore(Path(self.tmp.name))
        self.patches = [
            patch("app.services.library_flow.store", self.store),
            patch("app.services.library_flow.rebuild_index"),
        ]
        for item in self.patches:
            item.start()
        self.state = self.store.create("Проверка аренды", ["аренда"])
        self.state.status = CaseStatus.selected
        self.state.documents = [
            ProposedDocument(
                title="Инструкция НБРБ № 180 Об открытии и использовании счетов в банках",
                doc_type="инструкция",
                why_needed="счета",
                priority=1,
                selected=True,
                found_url="https://pravo.by/document/?guid=3871&p0=B22199999",
            )
        ]
        self.store.save(self.state)

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def _page(self, heading: str) -> dict:
        text = heading + "\n" + ("Статья 1. Норма акта. " * 40)
        lib = self.store.library_dir(self.state.case_id)
        lib.mkdir(parents=True, exist_ok=True)
        path = lib / "01_tmp.html"
        path.write_text(text, encoding="utf-8")
        txt = path.with_suffix(".txt")
        txt.write_text(text, encoding="utf-8")
        return {
            "local_path": str(path),
            "text_extract": str(txt),
            "url": "https://pravo.by/document/?guid=3871&p0=B22199999",
            "sha256": "abc",
            "bytes": len(text),
        }

    async def test_rejects_civil_code_body_for_instruction(self):
        from unittest.mock import AsyncMock, patch

        from app.services.library_flow import run_download

        async def fake_download(*_args, **_kwargs):
            return self._page("Гражданский кодекс Республики Беларусь")

        with (
            patch("app.services.library_flow.download_url", fake_download),
            patch("app.services.library_flow.find_candidate_urls", AsyncMock(return_value=[])),
            patch("app.services.library_flow.lookup_known_url", return_value=None),
        ):
            out = await run_download(self.state.case_id)
        doc = out.documents[0]
        self.assertEqual(doc.download_status, "failed")
        self.assertIn("не соответствует названию", doc.download_error or "")
        lib = self.store.library_dir(self.state.case_id)
        self.assertFalse(any(lib.iterdir()))

    async def test_accepts_page_with_the_selected_title(self):
        from pathlib import Path
        from unittest.mock import AsyncMock, patch

        from app.services.library_flow import run_download

        async def fake_download(*_args, **_kwargs):
            return self._page(
                "Инструкция Национального банка Республики Беларусь "
                "№ 180 Об открытии и использовании счетов в банках"
            )

        with (
            patch("app.services.library_flow.download_url", fake_download),
            patch("app.services.library_flow.find_candidate_urls", AsyncMock(return_value=[])),
            patch("app.services.library_flow.lookup_known_url", return_value=None),
        ):
            out = await run_download(self.state.case_id)
        doc = out.documents[0]
        self.assertEqual(doc.download_status, "ok")
        self.assertTrue(Path(doc.local_path).exists())

    async def test_does_not_fetch_catalogued_neighbour_code(self):
        from unittest.mock import AsyncMock, patch

        from app.services.library_flow import run_download

        fetched: list[str] = []

        async def fake_download(url, *_args, **_kwargs):
            fetched.append(url)
            return self._page("Гражданский кодекс Республики Беларусь")

        self.state.documents[0].found_url = (
            "https://pravo.by/document/?guid=3871&p0=hk9800218"
        )
        self.store.save(self.state)
        with (
            patch("app.services.library_flow.download_url", fake_download),
            patch("app.services.library_flow.find_candidate_urls", AsyncMock(return_value=[])),
            patch("app.services.library_flow.lookup_known_url", return_value=None),
        ):
            out = await run_download(self.state.case_id)
        self.assertEqual(fetched, [])
        self.assertEqual(out.documents[0].download_status, "failed")
        self.assertIn("другой акт", out.documents[0].download_error or "")


class TestIngestSkipsLeftovers(unittest.TestCase):
    def test_unselected_file_is_not_ingested(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch

        from app.models import CaseStatus
        from app.services.knowledge_ingest import ingest_library
        from app.storage import CaseStore

        with TemporaryDirectory() as tmp:
            store = CaseStore(Path(tmp))
            state = store.create("Проверка", ["аренда"])
            state.status = CaseStatus.ready
            selected = ProposedDocument(
                title="Положение о бухгалтерском учете аренды",
                doc_type="положение",
                why_needed="x",
                selected=True,
                download_status="ok",
            )
            state.documents = [selected]
            lib = store.library_dir(state.case_id)
            good = lib / "01_lease.html"
            good.write_text(
                "Положение о бухгалтерском учете аренды\n" + ("Статья 1. Аренда. " * 20),
                encoding="utf-8",
            )
            leftover = lib / "01_gk.html"
            leftover.write_text(
                "Гражданский кодекс Республики Беларусь\n" + ("Статья 625. " * 20),
                encoding="utf-8",
            )
            selected.local_path = str(good)
            store.save(state)
            with patch("app.services.knowledge_ingest.store", store):
                out = ingest_library(state.case_id)
            names = {item.filename for item in out.knowledge}
            self.assertIn(good.name, names)
            self.assertNotIn(leftover.name, names)


if __name__ == "__main__":
    unittest.main()
