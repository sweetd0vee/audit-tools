import unittest

from app.services.npa_identity import (
    page_matches_title,
    same_npa_title,
    titles_compatible,
)


def _page(heading: str) -> str:
    return heading + "\n" + ("Статья 1. Текст нормы. " * 40)


class TestSameNpaTitle(unittest.TestCase):
    def test_civil_code_short_and_full(self):
        self.assertTrue(
            same_npa_title(
                "Гражданский кодекс",
                "Гражданский кодекс Республики Беларусь",
            )
        )

    def test_generic_law_prefix_does_not_swallow_a_named_act(self):
        self.assertFalse(
            same_npa_title(
                "Закон Республики Беларусь",
                'Закон Республики Беларусь «О валютном регулировании и валютном контроле»',
            )
        )

    def test_different_codes(self):
        self.assertFalse(
            same_npa_title(
                "Гражданский кодекс Республики Беларусь",
                "Налоговый кодекс Республики Беларусь",
            )
        )

    def test_quoted_name_match(self):
        self.assertTrue(
            same_npa_title(
                'Закон «О валютном регулировании и валютном контроле»',
                'Закон Республики Беларусь от 09.06.2005 № 97-З '
                '"О валютном регулировании и валютном контроле"',
            )
        )


class TestTitlesCompatible(unittest.TestCase):
    def test_search_hit_for_other_code_is_rejected(self):
        self.assertFalse(
            titles_compatible(
                "Положение о бухгалтерском учете аренды",
                "Гражданский кодекс Республики Беларусь",
            )
        )

    def test_short_snippet_is_unknown_not_reject(self):
        self.assertTrue(
            titles_compatible(
                "Положение о бухгалтерском учете аренды",
                "Скачать PDF",
            )
        )

    def test_number_mismatch(self):
        self.assertFalse(
            titles_compatible(
                "Инструкция НБРБ № 180 Об открытии счетов",
                "Инструкция НБРБ № 38 Об открытии счетов",
            )
        )


class TestPageMatchesTitle(unittest.TestCase):
    def test_matching_heading(self):
        self.assertTrue(
            page_matches_title(
                "Положение о бухгалтерском учете аренды",
                _page("Положение о бухгалтерском учете аренды"),
            )
        )

    def test_civil_code_is_not_lease_position(self):
        self.assertFalse(
            page_matches_title(
                "Положение о бухгалтерском учете аренды",
                _page("Гражданский кодекс Республики Беларусь"),
            )
        )

    def test_gk_alias_requires_civil_code_heading(self):
        self.assertTrue(page_matches_title("ГК", _page("Гражданский кодекс Республики Беларусь")))
        self.assertFalse(
            page_matches_title(
                "ГК",
                _page("Инструкция НБРБ № 180 Об открытии и использовании счетов"),
            )
        )

    def test_wrong_instruction_number(self):
        self.assertFalse(
            page_matches_title(
                "Инструкция НБРБ № 180 Об открытии и использовании счетов в банках",
                _page(
                    "Инструкция Национального банка Республики Беларусь "
                    "№ 38 Об открытии и использовании счетов в банках"
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
