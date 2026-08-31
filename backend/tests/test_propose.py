import unittest

from app.services.known_sources import (
    KNOWN_NPA,
    catalog_act_by_number,
    catalog_prompt_block,
    lookup_known_url,
    match_catalog_act,
)
from app.services.ollama_client import (
    bind_documents_to_catalog,
    build_user_prompt,
    normalize_documents,
)


class TestCatalogPropose(unittest.TestCase):
    def test_prompt_lists_every_catalog_act(self):
        block = catalog_prompt_block()
        for i, act in enumerate(KNOWN_NPA, start=1):
            self.assertIn(f"{i}. {act.title}", block)
        text = build_user_prompt("Проверка аренды", ["аренда"])
        self.assertIn("Гражданский кодекс Республики Беларусь", text)
        self.assertIn("Каталог актов с официальным URL", text)
        self.assertIn('"n": 1', text)

    def test_number_maps_to_catalog_url(self):
        act = catalog_act_by_number(1)
        self.assertIsNotNone(act)
        assert act is not None
        self.assertEqual(act.title, KNOWN_NPA[0].title)
        self.assertIn("hk9800218", act.url)

    def test_bind_keeps_catalog_number_and_drops_invented(self):
        bound = bind_documents_to_catalog(
            [
                {"n": 1, "why_needed": "база", "priority": 1},
                {
                    "title": "Постановление СМ РБ О порядке определения базовой арендной ставки",
                    "why_needed": "ставка",
                    "priority": 1,
                },
            ]
        )
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["title"], KNOWN_NPA[0].title)
        self.assertEqual(bound[0]["found_url"], KNOWN_NPA[0].url)

    def test_bind_matches_alias_title(self):
        bound = bind_documents_to_catalog(
            [
                {
                    "title": "Положение о бухгалтерском учете аренды",
                    "why_needed": "лизинг",
                    "priority": 1,
                }
            ]
        )
        self.assertEqual(len(bound), 1)
        self.assertIn("W21833716", bound[0]["found_url"])
        self.assertEqual(
            bound[0]["title"],
            "Положение о бухгалтерском учете финансовой аренды (лизинга)",
        )

    def test_bind_dedupes_same_act(self):
        bound = bind_documents_to_catalog(
            [
                {"n": 1, "why_needed": "кодекс", "priority": 2},
                {
                    "title": "Гражданский кодекс Республики Беларусь (Книга первая)",
                    "why_needed": "аренда в ГК",
                    "priority": 1,
                },
            ]
        )
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["priority"], 1)
        self.assertEqual(bound[0]["why_needed"], "аренда в ГК")

    def test_normalize_rejects_all_unknown(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_documents(
                {
                    "topics": ["аренда"],
                    "documents": [
                        {
                            "title": "Закон Республики Беларусь «Об аренде»",
                            "why_needed": "нет в каталоге как отдельный акт",
                            "priority": 1,
                        }
                    ],
                },
                max_docs=15,
            )
        self.assertIn("catalog", str(ctx.exception).lower())

    def test_normalize_attaches_found_url(self):
        topics, docs = normalize_documents(
            {
                "topics": ["аренда"],
                "documents": [
                    {"n": 1, "why_needed": "ГК", "priority": 1},
                    {"n": 12, "why_needed": "указ", "priority": 1},
                ],
            },
            max_docs=15,
        )
        self.assertEqual(topics, ["аренда"])
        self.assertEqual(len(docs), 2)
        self.assertTrue(all(d.get("found_url") for d in docs))
        self.assertTrue(all(lookup_known_url(d["title"]) == d["found_url"] for d in docs))

    def test_every_catalog_title_looks_up_itself(self):
        for act in KNOWN_NPA:
            self.assertEqual(lookup_known_url(act.title), act.url, act.title)
            matched = match_catalog_act(act.title)
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched.url, act.url)


if __name__ == "__main__":
    unittest.main()
