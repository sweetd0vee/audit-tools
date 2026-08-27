import tempfile
import unittest
import zipfile
from pathlib import Path

from app.prompts import prompt
from app.services.conclusion_docx import (
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
        self.assertIn("сверк", obs_sections[0].observations[0].recommendation.lower())
        self.assertEqual(len(general), 1)
        self.assertEqual(general[0].roman, "IV")
        titles = [title for _, title in toc_entries(report)]
        self.assertEqual(titles[0], "Аудиторское мнение по итогам проверки.")
        self.assertEqual(titles[1], "Основные результаты аудита и итоговые аудиторские рекомендации.")
        self.assertTrue(any("общая информация" in t.lower() for t in titles))
        self.assertGreaterEqual(len(titles), 4)

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
            self.assertIn("АУДИТОРСКОЕ ЗАКЛЮЧЕНИЕ", texts)
            self.assertIn("Проверка аренды коммерческой недвижимости", texts)
            self.assertIn("Разделы аудиторского заключения", texts)
            self.assertIn("Аудиторское мнение по итогам проверки.", texts)
            self.assertIn("Основные результаты аудита и итоговые аудиторские рекомендации.", texts)
            self.assertIn("Раздел III.", texts)
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
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("word/document.xml").decode("utf-8")
            self.assertIn("Calibri", xml)
            self.assertIn("w:tbl", xml)


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
        self.assertIn("Общая информация", sections)
        self.assertIn("{hypotheses_block}", prompt("conclusion_user"))
        self.assertIn("подтверждённ", user)
        self.assertNotIn("{missing}", user)


if __name__ == "__main__":
    unittest.main()
