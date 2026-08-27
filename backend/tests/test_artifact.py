from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models import CaseState
from app.services.document_artifact import (
    ArtifactOutcome,
    ArtifactSpec,
    ComposeNotice,
    artifact_stale,
    case_stale_extra,
    complete_llm,
    event_result,
    reuse_artifact_events,
    run_llm_artifact_events,
    save_artifact_meta,
    sse_status,
    upstream_built_at,
)
from app.storage import CaseStore

SPEC = ArtifactSpec(
    meta_key="probe",
    directory="probes",
    file_prefix="probe",
    md_name="probe.md",
    sources_name="probe_sources.json",
    download_suffix="probe",
    docx_endpoint="/api/v1/cases/{case_id}/knowledge/probe.docx",
    md_endpoint="/api/v1/cases/{case_id}/knowledge/probe.md",
    docx_glob="probe_*.docx",
)


class TestSseHelpers(unittest.TestCase):
    def test_sse_status_shape(self):
        event = sse_status(12, "пишу")
        self.assertEqual(event["type"], "status")
        self.assertEqual(event["message"], "пишу")
        self.assertEqual(event["elapsed_ms"], 12)

    def test_case_stale_extra_and_upstream(self):
        state = CaseState(
            case_id="c1",
            inspection_name="Проверка аренды",
            keywords=["аренда"],
            meta={"brief": {"built_at": "t1"}, "program": {"built_at": "t2"}},
        )
        extra = case_stale_extra(state, **upstream_built_at(state, "brief", "program"), font="Calibri")
        self.assertEqual(extra["keywords"], ["аренда"])
        self.assertEqual(extra["inspection_name"], "Проверка аренды")
        self.assertEqual(extra["brief_built_at"], "t1")
        self.assertEqual(extra["program_built_at"], "t2")
        self.assertEqual(extra["font"], "Calibri")


class TestReuseArtifact(unittest.TestCase):
    def test_force_or_stale_rebuilds(self):
        self.assertIsNone(
            reuse_artifact_events(
                "c1", SPEC, force=True, stale=False, already_message="готово", elapsed_ms=1
            )
        )
        self.assertIsNone(
            reuse_artifact_events(
                "c1", SPEC, force=False, stale=True, already_message="готово", elapsed_ms=1
            )
        )

    def test_fresh_file_is_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            docx = store.case_dir(state.case_id) / "probes"
            docx.mkdir(parents=True, exist_ok=True)
            primary = docx / f"probe_file_{state.case_id}.docx"
            primary.write_bytes(b"PK")
            md = docx / "probe.md"
            md.write_text("ok", encoding="utf-8")
            with patch("app.services.document_artifact.store", store):
                save_artifact_meta(
                    state,
                    SPEC,
                    docx=primary,
                    md=md,
                    sources=[],
                    body="ok",
                    extra={"schema": 1, "built_elapsed_ms": 123000},
                )
                events = reuse_artifact_events(
                    state.case_id,
                    SPEC,
                    force=False,
                    stale=artifact_stale(store.get(state.case_id), SPEC),
                    already_message="уже собрано",
                    elapsed_ms=9,
                )
            self.assertIsNotNone(events)
            assert events is not None
            self.assertEqual(events[0]["type"], "status")
            self.assertEqual(events[0]["message"], "уже собрано")
            self.assertEqual(events[1]["type"], "result")
            self.assertEqual(events[1]["digest"], [])
            self.assertTrue(events[1]["ready"])
            self.assertTrue(events[1]["reused"])
            self.assertEqual(events[1]["built_elapsed_ms"], 123000)
            self.assertEqual(events[1]["elapsed_ms"], 9)


class TestCompleteLlm(unittest.IsolatedAsyncioTestCase):
    async def test_wraps_and_rejects_empty(self):
        async def boom():
            raise RuntimeError("timeout")

        with self.assertRaises(ValueError) as wrapped:
            await complete_llm(boom(), fail="Модель не собрала x")
        self.assertIn("Модель не собрала x: timeout", str(wrapped.exception))

        async def blank():
            return "  "

        with self.assertRaises(ValueError) as empty:
            await complete_llm(blank(), fail="fail", empty="пусто")
        self.assertEqual(str(empty.exception), "пусто")


class TestRunLlmArtifactEvents(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_blocks_before_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            composed = False

            async def compose(_state, _ctx):
                nonlocal composed
                composed = True
                return "text"

            def inspect(_state):
                raise ValueError("нет гипотез")

            with patch("app.services.document_artifact.store", store):
                with self.assertRaises(ValueError) as err:
                    await event_result(
                        run_llm_artifact_events(
                            state.case_id,
                            SPEC,
                            force=True,
                            start_message="старт",
                            already_message="уже",
                            writing_message="файл",
                            load_state=store.get,
                            is_stale=lambda _s: True,
                            inspect=inspect,
                            compose=compose,
                            write=lambda *_a: ArtifactOutcome(body="x", sources=[]),
                            compose_fail="fail",
                            compose_message="пишу",
                        ),
                        "не собрано",
                    )
            self.assertIn("нет гипотез", str(err.exception))
            self.assertFalse(composed)

    async def test_writes_files_and_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            calls: list[str] = []

            async def compose(_state, ctx):
                calls.append(ctx)
                return "тело модели"

            def prepare(_state):
                return "источники"

            def write(state, paths, raw, ctx):
                self.assertEqual(raw, "тело модели")
                self.assertEqual(ctx, "источники")
                paths.md.write_text(raw, encoding="utf-8")
                paths.primary.write_bytes(b"PK")
                return ArtifactOutcome(
                    body=raw,
                    sources=[{"n": 1, "title": "ГК"}],
                    extra={"schema": 1},
                    digest=["- ГК"],
                )

            with patch("app.services.document_artifact.store", store):
                result = await event_result(
                    run_llm_artifact_events(
                        state.case_id,
                        SPEC,
                        force=True,
                        start_message="старт",
                        already_message="уже",
                        writing_message="файл",
                        load_state=store.get,
                        is_stale=lambda _s: True,
                        prepare_message="отбор",
                        prepare=prepare,
                        compose=compose,
                        write=write,
                        compose_fail="Модель не собрала probe",
                        compose_message="пишу",
                        empty_error="пусто",
                    ),
                    "не собрано",
                )

            self.assertEqual(calls, ["источники"])
            self.assertEqual(result["type"], "result")
            self.assertEqual(result["digest"], ["- ГК"])
            self.assertEqual(result["citations"], 1)
            self.assertTrue(result["ready"])
            saved = store.get(state.case_id)
            probe = saved.meta.get("probe") or {}
            self.assertEqual(probe.get("schema"), 1)
            self.assertIsInstance(probe.get("built_elapsed_ms"), int)
            self.assertGreaterEqual(probe["built_elapsed_ms"], 0)
            sources = Path((saved.meta["probe"]["docx_path"])).with_name("probe_sources.json")
            self.assertTrue(sources.exists())

    async def test_compose_notice_emits_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            statuses: list[str] = []

            async def compose(_state, _ctx):
                yield ComposeNotice("дописываю наблюдения")
                yield "тело"

            def write(_state, paths, raw, _ctx):
                paths.md.write_text(raw, encoding="utf-8")
                paths.primary.write_bytes(b"PK")
                return ArtifactOutcome(body=raw, sources=[], digest=["- x"])

            with patch("app.services.document_artifact.store", store):
                async for event in run_llm_artifact_events(
                    state.case_id,
                    SPEC,
                    force=True,
                    start_message="старт",
                    already_message="уже",
                    writing_message="файл",
                    load_state=store.get,
                    is_stale=lambda _s: True,
                    compose=compose,
                    write=write,
                    compose_fail="fail",
                    compose_message="пишу",
                ):
                    if event.get("type") == "status":
                        statuses.append(event["message"])

            self.assertIn("дописываю наблюдения", statuses)
            self.assertEqual(statuses[-1], "файл")

    async def test_heartbeat_status_while_compose_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CaseStore(root=Path(tmp))
            state = store.create("Проверка аренды", ["аренда"])
            statuses: list[str] = []

            async def compose(_state, _ctx):
                await asyncio.sleep(0.16)
                return "тело"

            def write(_state, paths, raw, _ctx):
                paths.md.write_text(raw, encoding="utf-8")
                paths.primary.write_bytes(b"PK")
                return ArtifactOutcome(body=raw, sources=[])

            with patch("app.services.document_artifact.store", store):
                async for event in run_llm_artifact_events(
                    state.case_id,
                    SPEC,
                    force=True,
                    start_message="старт",
                    already_message="уже",
                    writing_message="файл",
                    load_state=store.get,
                    is_stale=lambda _s: True,
                    compose=compose,
                    write=write,
                    compose_fail="fail",
                    compose_message="пишу",
                    heartbeat_sec=0.05,
                ):
                    if event.get("type") == "status":
                        statuses.append(event["message"])

            self.assertGreaterEqual(statuses.count("пишу"), 2)
            self.assertEqual(statuses[-1], "файл")


class TestKnowledgeSplit(unittest.TestCase):
    def test_facades_are_not_reexport_stubs(self):
        import app.services.knowledge_ask as ask_mod
        import app.services.knowledge_index as index_mod
        import app.services.knowledge_ingest as ingest_mod
        import app.services.knowledge_owui as owui_mod
        import app.services.knowledge_summarize as summarize_mod

        self.assertIn("ingest_library", ingest_mod.__dict__)
        self.assertTrue(inspect.isfunction(ingest_mod.ingest_library))
        self.assertTrue(inspect.isfunction(index_mod.rebuild_index))
        self.assertTrue(inspect.isfunction(summarize_mod.summarize_item))
        self.assertTrue(inspect.iscoroutinefunction(ask_mod.ask))
        self.assertTrue(inspect.isfunction(owui_mod.export_pack_files))
        source = inspect.getsource(ingest_mod)
        self.assertNotIn("from app.services.knowledge_flow import", source)


if __name__ == "__main__":
    unittest.main()
