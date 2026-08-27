import tempfile
import unittest
import zipfile
from pathlib import Path

from app.prompts import prompt
from app.services.conclusion_docx import (
    _COVER_IMAGE,
    _cover_year,
    parse_conclusion_markdown,
    toc_entries,
    write_conclusion_docx,
)
from app.services.opinion_flow import FONT_CALIBRI, FONT_TIMES, parse_opinion_font_flag


def _row(n: int, priority: str = "средний") -> dict:
    return {
        "n": str(n),
        "hypothesis": f"Гипотеза {n} о риске учёта",
        "priority": priority,
        "assertion": "полнота",
        "risk": "риск недостоверности",
        "why_risk": "нет сверки",
        "how_to_test": "сверка договоров",
        "evidence_request": "договоры",
        "working_paper": "Доработать сверку регистров с договорами.",
        "npa_criteria": "НК РБ",
        "plan_sections": "учёт",
    }


SAMPLE_MD = """
## Содержание
III. Учёт аренды и сверка договоров
IV. Общая информация об аудиторской проверке

## Раздел III. Учёт аренды и сверка договоров

В ходе проверки рассмотрены вопросы отражения арендных платежей.

### Наблюдение 3.1. Неполная сверка договоров аренды
существенность: высокий
гипотеза: 1

По подтверждённой гипотезе отмечается риск неполного отражения платежей по договорам аренды.
Контроль сверки регистров с договорами требует усиления.

рекомендация:
Обеспечить регулярную сверку регистров аренды с действующими договорами.

### Наблюдение 3.2. Своевременность отражения НДС
существенность: средний
гипотеза: 3

Отмечается риск несвоевременного отражения НДС по арендным операциям.

рекомендация:
Актуализировать контрольные процедуры по срокам отражения НДС.

## Раздел IV. Общая информация об аудиторской проверке

### Аудируемый период
2025

### Вид аудита
Тематическая аудиторская проверка
"""


class TestConclusionFont(unittest.TestCase):
    def test_parse_flags(self):
        self.assertEqual(parse_opinion_font_flag("аудиторское заключение -c"), FONT_CALIBRI)
        self.assertEqual(parse_opinion_font_flag("аудиторское заключение -t заново"), FONT_TIMES)
        self.assertEqual(parse_opinion_font_flag("аудиторское заключение"), FONT_TIMES)


class TestConclusionParse(unittest.TestCase):
    def test_parses_observations_and_skips_i_ii(self):
        report = parse_conclusion_markdown(
            SAMPLE_MD,
            hypotheses=[_row(1, "высокий"), _row(3, "средний")],
            period="2025",
        )
        obs_sections = [s for s in report.sections if s.kind == "observations"]
        general = [s for s in report.sections if s.kind == "general"]
        self.assertEqual(len(obs_sections), 1)
        self.assertEqual(len(obs_sections[0].observations), 2)
        self.assertEqual(obs_sections[0].observations[0].number, "3.1")
        self.assertEqual(obs_sections[0].observations[0].materiality, "высокий")
        self.assertNotIn("существенность", obs_sections[0].observations[0].body.lower())
        self.assertIn("сверк", obs_sections[0].observations[0].recommendation.lower())
        self.assertEqual(len(general), 1)
        self.assertEqual(general[0].roman, "IV")
        self.assertEqual(obs_sections[0].roman, "III")
        self.assertIn("принципам налогообложения", obs_sections[0].title.lower())
        titles = [title for _, title in toc_entries(report)]
        self.assertEqual(
            titles,
            [
                "Аудиторское мнение по итогам проверки.",
                "Основные результаты аудита и итоговые аудиторские рекомендации.",
                "Оценка соответствия деятельности принципам налогообложения и защиты прав плательщиков.",
                "Общая информация об аудиторской проверке.",
            ],
        )
        self.assertEqual([roman for roman, _ in toc_entries(report)], ["I", "II", "III", "IV"])

    def test_merges_extra_observation_sections_into_iii(self):
        md = """
## Содержание
III. Тема А
IV. Тема Б
V. Общая информация об аудиторской проверке

## Раздел III. Тема А
### Наблюдение 3.1. Первое
существенность: высокий
гипотеза: 1
Текст первого наблюдения.
рекомендация:
Сделать первое.

## Раздел IV. Тема Б
### Наблюдение 4.1. Второе
существенность: средний
гипотеза: 3
Текст второго наблюдения.
рекомендация:
Сделать второе.

## Раздел V. Общая информация об аудиторской проверке
### Аудируемый период
2025
"""
        report = parse_conclusion_markdown(
            md,
            hypotheses=[_row(1, "высокий"), _row(3, "средний")],
            period="2025",
        )
        obs_sections = [s for s in report.sections if s.kind == "observations"]
        self.assertEqual(len(obs_sections), 1)
        self.assertEqual(len(obs_sections[0].observations), 2)
        self.assertEqual(obs_sections[0].observations[0].number, "3.1")
        self.assertEqual(obs_sections[0].observations[1].number, "3.2")
        self.assertEqual(
            [title for _, title in toc_entries(report)][2],
            "Оценка соответствия деятельности принципам налогообложения и защиты прав плательщиков.",
        )

    def test_fallback_uses_all_hypotheses(self):
        report = parse_conclusion_markdown(
            "просто текст без структуры",
            hypotheses=[_row(1, "высокий"), _row(2, "низкий")],
            period="2024",
        )
        obs = report.sections[0].observations
        self.assertEqual(len(obs), 2)
        self.assertEqual(obs[0].materiality, "высокий")
        self.assertEqual(obs[1].materiality, "низкий")


class TestConclusionDocx(unittest.TestCase):
    def test_cover_year_and_background_asset(self):
        self.assertTrue(_COVER_IMAGE.exists(), _COVER_IMAGE)
        self.assertEqual(_cover_year("2025"), "2025")
        self.assertEqual(_cover_year("2024 год и 3 квартала 2025 года"), "2025")

    def test_structure_font_and_observation_box(self):
        report = parse_conclusion_markdown(
            SAMPLE_MD,
            hypotheses=[_row(1, "высокий"), _row(3, "средний")],
            period="2025",
        )
        opinion = "## Аудиторское мнение\nПо подтверждённым гипотезам отмечается риск учёта аренды."
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "zakluchenie.docx"
            write_conclusion_docx(
                path,
                inspection_name="Проверка аренды коммерческой недвижимости",
                period="2025",
                case_id="abc123",
                opinion_body=opinion,
                report=report,
                font=FONT_CALIBRI,
            )
            self.assertTrue(path.exists())
            from docx import Document

            doc = Document(str(path))
            texts = "\n".join(p.text for p in doc.paragraphs)
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8")
                media = [n for n in zf.namelist() if n.startswith("word/media/")]
            self.assertIn("Аудиторское заключение", xml)
            self.assertIn("Проверка аренды коммерческой недвижимости", xml)
            self.assertIn("Минск 2025", xml)
            self.assertIn("Департамент внутреннего аудита", xml)
            self.assertIn("Разделы аудиторского заключения", texts)
            self.assertIn("I.\tАудиторское мнение по итогам проверки.", texts)
            self.assertIn("II.\tОсновные результаты аудита и итоговые аудиторские рекомендации.", texts)
            self.assertIn(
                "III.\tОценка соответствия деятельности принципам налогообложения и защиты прав плательщиков.",
                texts,
            )
            self.assertIn("IV.\tОбщая информация об аудиторской проверке.", texts)
            self.assertNotIn("Раздел III.", texts)
            self.assertNotIn("Учёт аренды и сверка договоров", texts)
            self.assertIn("Уровень существенности:", texts)
            self.assertIn("высокий", texts)
            self.assertIn("Аудитор:", texts)
            self.assertIn("Объект аудита:", texts)
            self.assertIn("Аудиторская рекомендация:", texts)
            self.assertIn("Срок –", texts)
            self.assertIn("Общая информация об аудиторской проверке.", texts)
            self.assertIn("По подтверждённым гипотезам отмечается риск учёта аренды.", texts)
            self.assertNotIn("Основные нарушения и недостатки", texts)
            boxes = [t for t in doc.tables if "Наблюдение" in t.cell(0, 0).text]
            self.assertGreaterEqual(len(boxes), 2)
            self.assertIn("3.1", boxes[0].cell(0, 0).text)
            self.assertTrue(boxes[0].cell(0, 1).paragraphs[0].runs[0].italic)
            from docx.oxml.ns import qn

            tbl_borders = boxes[0]._tbl.tblPr.find(qn("w:tblBorders"))
            self.assertIsNotNone(tbl_borders)
            self.assertEqual(tbl_borders.find(qn("w:top")).get(qn("w:val")), "nil")
            self.assertEqual(tbl_borders.find(qn("w:left")).get(qn("w:val")), "nil")
            boxed = {
                p.text.strip(): p._p.find(qn("w:pPr")).find(qn("w:pBdr")) is not None
                for p in doc.paragraphs
                if p._p.find(qn("w:pPr")) is not None
                and p._p.find(qn("w:pPr")).find(qn("w:pBdr")) is not None
            }
            boxed_text = "\n".join(boxed)
            self.assertIn("Уровень существенности:", boxed_text)
            self.assertIn("Аудитор:", boxed_text)
            self.assertIn("Объект аудита:", boxed_text)
            self.assertIn("Руководитель объекта аудита:", boxed_text)
            self.assertIn("Аудиторская рекомендация:", boxed_text)
            self.assertIn("Обеспечить регулярную сверку регистров аренды с действующими договорами.", boxed_text)
            self.assertIn("Срок –", boxed_text)
            self.assertIn("Calibri", xml)
            self.assertIn("w:tbl", xml)
            self.assertIn("w:pBdr", xml)
            self.assertIn('behindDoc="1"', xml)
            self.assertTrue(any(n.endswith(".png") for n in media), media)
            self.assertTrue(doc.sections[0].different_first_page_header_footer)
            self.assertLess(xml.find("Наблюдение 3.1"), xml.find("По подтверждённой гипотезе"))
            self.assertLess(
                xml.find("По подтверждённой гипотезе"),
                xml.find("Аудиторская рекомендация"),
            )


class TestConclusionPrompts(unittest.TestCase):
    def test_prompts_skip_section_ii_and_require_template(self):
        system = prompt("conclusion_system")
        sections = prompt("conclusion_sections")
        user = prompt(
            "conclusion_user",
            inspection="Проверка аренды",
            keywords="аренда",
            period="2025",
            document_catalog="",
            hypotheses_block="1. тест",
            opinion_block="",
            program_block="",
            brief_block="",
            total_block="",
            cards_block="",
            fragments="",
            sections=sections,
        )
        self.assertIn("подтверждённ", system)
        self.assertIn("Раздел II", system)
        self.assertIn("существенн", system)
        self.assertIn("Наблюдение 3.1", sections)
        self.assertIn("принципам налогообложения", sections)
        self.assertIn("Общая информация", sections)
        self.assertNotIn("сгруппируй", sections.lower())
        self.assertIn("{hypotheses_block}", prompt("conclusion_user"))
        self.assertIn("подтверждённ", user)
        self.assertNotIn("{missing}", user)


if __name__ == "__main__":
    unittest.main()
