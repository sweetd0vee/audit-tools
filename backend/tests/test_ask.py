from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models import KnowledgeItem
from app.storage import CaseStore


class TestAskRefuse(unittest.IsolatedAsyncioTestCase):
    async def test_empty_evidence_refuses_without_llm(self):
        from app.services import knowledge_ask as kf
        from app.services import knowledge_index as ki

        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            item = KnowledgeItem(
                title="ГК",
                filename="gk.txt",
                local_path="gk.txt",
                source="downloaded",
                extract_status="ok",
            )
            state.knowledge = [item]
            store.save(state)
            index = {
                "embed_model": "test",
                "chunks": [
                    {
                        "id": f"{item.id}:0",
                        "item_id": item.id,
                        "title": "ГК",
                        "filename": "gk.txt",
                        "text": "## Статья 1. Преамбула\nканцелярия " * 20,
                        "embedding": [],
                    }
                ],
            }
            (store.case_dir(state.case_id) / "knowledge_index.json").write_text(
                json.dumps(index, ensure_ascii=False),
                encoding="utf-8",
            )

            chat = AsyncMock(return_value="не должен вызываться")
            with (
                patch.object(kf, "store", store),
                patch.object(ki, "store", store),
                patch.object(kf, "retrieve_for_ask", AsyncMock(return_value=[])),
                patch.object(kf, "embed_texts", AsyncMock(return_value=[[0.0, 1.0]])),
                patch.object(kf, "chat_complete", chat),
            ):
                result = await kf.ask(state.case_id, "статья 999 ГК")

            self.assertTrue(result["refused"])
            self.assertEqual(result["refuse_reason"], "no_evidence")
            self.assertEqual(result["sources"], [])
            self.assertFalse(result["used_summaries"])
            self.assertIn("нет фрагмента", result["answer"].lower())
            chat.assert_not_awaited()
            trail = store.case_dir(state.case_id) / "trail" / "ask.jsonl"
            self.assertTrue(trail.exists())
            line = json.loads(trail.read_text(encoding="utf-8").splitlines()[0])
            self.assertTrue(line["refused"])
            self.assertEqual(line["question"], "статья 999 ГК")

    async def test_hits_do_not_inject_summaries(self):
        from app.services import knowledge_ask as kf
        from app.services import knowledge_index as ki

        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            item = KnowledgeItem(
                id="gkitem",
                title="Гражданский кодекс",
                filename="gk.txt",
                local_path="gk.txt",
                source="downloaded",
                extract_status="ok",
                summary="Карточка: якобы статья 777 про выдумку.",
            )
            state.knowledge = [item]
            store.save(state)
            index = {
                "embed_model": "test",
                "chunks": [
                    {
                        "id": "gkitem:0",
                        "item_id": "gkitem",
                        "title": "Гражданский кодекс",
                        "filename": "gk.txt",
                        "text": "## Статья 625. Договор аренды\nрегистрация договора",
                        "embedding": [1.0],
                        "rerank_score": 0.9,
                    }
                ],
            }
            (store.case_dir(state.case_id) / "knowledge_index.json").write_text(
                json.dumps(index, ensure_ascii=False),
                encoding="utf-8",
            )
            evidence = [dict(index["chunks"][0])]
            captured: dict[str, str] = {}

            async def fake_chat(system, user, **kwargs):
                captured["user"] = user
                captured["system"] = system
                return "По ст. 625 нужна регистрация."

            with (
                patch.object(kf, "store", store),
                patch.object(ki, "store", store),
                patch.object(kf, "retrieve_for_ask", AsyncMock(return_value=evidence)),
                patch.object(kf, "chat_complete", fake_chat),
            ):
                result = await kf.ask(state.case_id, "ст. 625 регистрация")

            self.assertFalse(result["refused"])
            self.assertEqual(len(result["sources"]), 1)
            self.assertNotIn("777", captured["user"])
            self.assertNotIn("карточка", captured["user"].lower())
            self.assertNotIn("выдумк", captured["user"].lower())
            self.assertIn("625", captured["user"])


class TestRagEvalGold(unittest.IsolatedAsyncioTestCase):
    async def test_twenty_gold_questions(self):
        import sys
        from pathlib import Path

        backend = str(Path(__file__).resolve().parents[1])
        if backend not in sys.path:
            sys.path.insert(0, backend)
        from eval_rag import GOLD, run_eval

        rows = await run_eval()
        failed = [r for r in rows if not r["ok"]]
        self.assertEqual(len(rows), 20)
        self.assertEqual(len(GOLD), 20)
        self.assertEqual(failed, [], msg=failed)


if __name__ == "__main__":
    unittest.main()
