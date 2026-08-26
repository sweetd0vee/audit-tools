import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from app.services.hypotheses_flow import parse_hypotheses_payload
from app.services.hypotheses_xlsx import write_hypotheses_xlsx


def _row(i: int, priority: str) -> dict:
    return {
        "hypothesis": f"Гипотеза {i}",
        "assertion": "полнота",
        "risk": "недоплата",
        "plan_sections": "учёт аренды",
        "npa_criteria": "НК РБ [1]",
        "why_risk": "налоговый риск",
        "how_to_test": "сверка регистров",
        "evidence_request": "карточки счетов",
        "working_paper": "реестр отклонений",
        "priority": priority,
        "basis": "НПА [1]",
    }


class TestParseHypotheses(unittest.TestCase):
    def test_parses_object_with_eight_rows(self):
        payload = {
            "notes": "Черновик",
            "hypotheses": [_row(i, "высокий" if i <= 3 else "средний") for i in range(1, 9)],
        }
        rows, notes = parse_hypotheses_payload(payload)
        self.assertEqual(notes, "Черновик")
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["priority"], "высокий")

    def test_sorts_high_then_medium_then_low(self):
        priorities = [
            "средний",
            "низкий",
            "высокий",
            "средний",
            "низкий",
            "высокий",
            "средний",
            "высокий",
        ]
        payload = {"hypotheses": [_row(i, p) for i, p in enumerate(priorities, start=1)]}
        rows, _ = parse_hypotheses_payload(payload)
        self.assertEqual([r["priority"] for r in rows], ["высокий"] * 3 + ["средний"] * 3 + ["низкий"] * 2)
        self.assertEqual([r["n"] for r in rows], [str(i) for i in range(1, 9)])
        self.assertEqual(rows[0]["hypothesis"], "Гипотеза 3")
        self.assertEqual(rows[3]["hypothesis"], "Гипотеза 1")
        self.assertEqual(rows[6]["hypothesis"], "Гипотеза 2")

    def test_rejects_too_few(self):
        with self.assertRaises(ValueError):
            parse_hypotheses_payload({"hypotheses": [{"hypothesis": "одна"}]})


class TestHypothesesXlsx(unittest.TestCase):
    def test_writes_workbook_with_priority_column_and_sort(self):
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
                "priority": ["низкий", "средний", "высокий"][i % 3],
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
            wb = load_workbook(path)
            ws = wb["Гипотезы"]
            headers = [cell.value for cell in ws[1]]
            self.assertEqual(headers[0], "№")
            self.assertEqual(headers[1], "Гипотеза")
            self.assertEqual(headers[2], "Приоритет")
            self.assertNotIn("Утверждение", headers)
            self.assertEqual(headers.count("Приоритет"), 1)
            priorities = [ws.cell(row, 3).value for row in range(2, ws.max_row + 1)]
            self.assertEqual(
                priorities,
                ["высокий"] * 3 + ["средний"] * 3 + ["низкий"] * 2,
            )
            self.assertEqual(ws.cell(2, 1).value, 1)
            self.assertEqual(ws.cell(2, 2).value, "Гипотеза 2")


if __name__ == "__main__":
    unittest.main()
