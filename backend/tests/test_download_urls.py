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
        doc = "https://pravo.by/document/?guid=3871&p0=H11300057"
        self.assertGreater(score_url(doc, title), score_url(news, title))

    def test_banking_code_not_used_for_unrelated_instruction(self):
        title = "Инструкция НБРБ О порядке проведения внутреннего аудита в банках"
        code = "https://pravo.by/document/?guid=3871&p0=hk0000441"
        other = "https://etalonline.by/document/?regnum=B21428262"
        self.assertLess(score_url(code, title), score_url(other, title))
        self.assertLess(score_url(code, title), -40)

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


if __name__ == "__main__":
    unittest.main()
