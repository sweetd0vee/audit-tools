import tempfile
import unittest
from pathlib import Path

from app.services.hypotheses_flow import parse_hypotheses_payload
from app.services.hypotheses_xlsx import write_hypotheses_xlsx


class TestParseHypotheses(unittest.TestCase):
    def test_parses_object_with_eight_rows(self):
        payload = {
            "notes": "Черновик",
            "hypotheses": [
                {
                    "hypothesis": f"Гипотеза {i}",
                    "assertion": "полнота",
                    "risk": "недоплата",
                    "plan_sections": "учёт аренды",
                    "npa_criteria": "НК РБ [1]",
                    "why_risk": "налоговый риск",
                    "how_to_test": "сверка регистров",
                    "evidence_request": "карточки счетов",
                    "working_paper": "реестр отклонений",
                    "priority": "высокий" if i <= 3 else "средний",
                    "basis": "НПА [1]",
                }
                for i in range(1, 9)
            ],
        }
        rows, notes = parse_hypotheses_payload(payload)
        self.assertEqual(notes, "Черновик")
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["priority"], "высокий")

    def test_rejects_too_few(self):
        with self.assertRaises(ValueError):
            parse_hypotheses_payload({"hypotheses": [{"hypothesis": "одна"}]})


class TestHypothesesXlsx(unittest.TestCase):
    def test_writes_workbook(self):
        rows = [
            {
                "hypothesis": f"Гипотеза {i}",
                "assertion": "существование",
                "risk": "риск",
                "plan_sections": "раздел 7",
                "npa_criteria": "ГК РБ",
                "why_risk": "почему",
                "how_to_test": "как",
                "evidence_request": "договоры",
                "working_paper": "WP",
                "priority": "средний",
                "basis": "программа",
            }
            for i in range(1, 9)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gipotezy.xlsx"
            write_hypotheses_xlsx(
                path,
                inspection_name="Проверка аренды",
                period="2025",
                keywords=["аренда", "НДС"],
                case_id="abc123",
                rows=rows,
                notes="тест",
            )
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 2000)


if __name__ == "__main__":
    unittest.main()
