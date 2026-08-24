import unittest

from app.models import ProposedDocument
from app.services.downloader import usable_url
from app.services.known_sources import lookup_known_url
from app.services.library_flow import download_candidates


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
        self.assertIn("pravo.by", url)

    def test_nbrb_instruction_is_not_minfin_auditor_page(self):
        title = (
            "Инструкция Национального банка Республики Беларусь "
            'от 18.06.2021 № 180 "Об открытии и использовании счетов в банках"'
        )
        self.assertIsNone(lookup_known_url(title))

    def test_civil_code_still_matches(self):
        self.assertIn("hk9800218", lookup_known_url("Гражданский кодекс Республики Беларусь") or "")


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
        self.assertTrue(candidates[0][0].startswith("https://pravo.by/document/"))

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


if __name__ == "__main__":
    unittest.main()
