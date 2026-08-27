"""Offline RAG eval: retrieval + refuse gate, no Ollama.

Usage from backend/:  python eval_rag.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Mini library: 4 acts, distinctive wording so BM25/heading can score without embeddings.
CORPUS: list[dict] = [
    {
        "id": "gk:0",
        "item_id": "gk",
        "title": "Гражданский кодекс Республики Беларусь",
        "filename": "gk.txt",
        "text": (
            "## Статья 577. Понятие договора\n"
            "Договором признается соглашение двух или нескольких лиц об установлении "
            "гражданских прав и обязанностей."
        ),
        "embedding": [],
    },
    {
        "id": "gk:1",
        "item_id": "gk",
        "title": "Гражданский кодекс Республики Беларусь",
        "filename": "gk.txt",
        "text": (
            "## Статья 625. Договор аренды\n"
            "По договору аренды арендодатель обязуется предоставить арендатору имущество "
            "за плату во временное владение и пользование. Договор аренды недвижимого "
            "имущества подлежит государственной регистрации."
        ),
        "embedding": [],
    },
    {
        "id": "gk:2",
        "item_id": "gk",
        "title": "Гражданский кодекс Республики Беларусь",
        "filename": "gk.txt",
        "text": (
            "## Статья 626. Арендная плата\n"
            "Арендатор обязан своевременно вносить арендную плату. Порядок, условия и "
            "сроки внесения арендной платы определяются договором аренды."
        ),
        "embedding": [],
    },
    {
        "id": "gk:3",
        "item_id": "gk",
        "title": "Гражданский кодекс Республики Беларусь",
        "filename": "gk.txt",
        "text": (
            "## Статья 630. Предоставление имущества арендатору\n"
            "Арендодатель обязан предоставить имущество в состоянии, соответствующем "
            "условиям договора аренды и назначению имущества."
        ),
        "embedding": [],
    },
    {
        "id": "val:0",
        "item_id": "val",
        "title": "Закон Республики Беларусь О валютном регулировании и валютном контроле",
        "filename": "valuta.txt",
        "text": (
            "## Статья 10. Валютные операции\n"
            "Валютные операции между резидентами и нерезидентами осуществляются "
            "в порядке, установленном Национальным банком."
        ),
        "embedding": [],
    },
    {
        "id": "val:1",
        "item_id": "val",
        "title": "Закон Республики Беларусь О валютном регулировании и валютном контроле",
        "filename": "valuta.txt",
        "text": (
            "## Статья 12. Репатриация валютной выручки\n"
            "Резиденты обязаны обеспечить зачисление валютной выручки на счета в банках "
            "Республики Беларусь в сроки, установленные договором. "
            "См. также статью 625 Гражданского кодекса про аренду."
        ),
        "embedding": [],
    },
    {
        "id": "nbrb:0",
        "item_id": "nbrb",
        "title": "Инструкция Национального банка Республики Беларусь № 38",
        "filename": "instr38.txt",
        "text": (
            "Пункт 3.2. Банк идентифицирует валютную операцию клиента и отражает её "
            "в регистре валютного контроля не позднее следующего рабочего дня."
        ),
        "embedding": [],
    },
    {
        "id": "nk:0",
        "item_id": "nk",
        "title": "Налоговый кодекс Республики Беларусь",
        "filename": "nk.txt",
        "text": (
            "## Статья 93. Объект налогообложения НДС\n"
            "Объектом налогообложения налогом на добавленную стоимость признаются обороты "
            "по реализации товаров, работ, услуг на территории Республики Беларусь."
        ),
        "embedding": [],
    },
]


GOLD: list[dict] = [
    {
        "id": "hit-article-625",
        "kind": "article_number",
        "question": "Что говорит статья 625 ГК о договоре аренды?",
        "expect_refuse": False,
        "must_article": "625",
        "must_filename": "gk.txt",
        "must_contain": "регистрации",
    },
    {
        "id": "hit-st-dot",
        "kind": "article_number",
        "question": "ст. 626 арендная плата",
        "expect_refuse": False,
        "must_article": "626",
        "must_filename": "gk.txt",
        "must_contain": "арендную плату",
    },
    {
        "id": "hit-paraphrase-lease",
        "kind": "paraphrase",
        "question": "Нужна ли государственная регистрация договора аренды недвижимости?",
        "expect_refuse": False,
        "must_article": "625",
        "must_filename": "gk.txt",
        "must_contain": "регистрации",
    },
    {
        "id": "hit-paraphrase-rent",
        "kind": "paraphrase",
        "question": "Кто и когда вносит плату за пользование имуществом по аренде?",
        "expect_refuse": False,
        "must_filename": "gk.txt",
        "must_contain": "арендн",
    },
    {
        "id": "hit-heading-577",
        "kind": "article_number",
        "question": "статья 577 понятие договора",
        "expect_refuse": False,
        "must_article": "577",
        "must_filename": "gk.txt",
    },
    {
        "id": "hit-630-condition",
        "kind": "article_number",
        "question": "ст. 630 в каком состоянии передают имущество",
        "expect_refuse": False,
        "must_article": "630",
        "must_contain": "состоянии",
    },
    {
        "id": "hit-val-12",
        "kind": "article_number",
        "question": "статья 12 репатриация валютной выручки",
        "expect_refuse": False,
        "must_article": "12",
        "must_filename": "valuta.txt",
        "must_contain": "зачисление",
    },
    {
        "id": "hit-val-10",
        "kind": "article_number",
        "question": "ст. 10 валютные операции с нерезидентом",
        "expect_refuse": False,
        "must_article": "10",
        "must_filename": "valuta.txt",
    },
    {
        "id": "hit-punkt-32",
        "kind": "punkt",
        "question": "пункт 3.2 инструкции НБРБ про регистр валютного контроля",
        "expect_refuse": False,
        "must_filename": "instr38.txt",
        "must_contain": "регистре",
    },
    {
        "id": "hit-nds-93",
        "kind": "article_number",
        "question": "статья 93 НК объект НДС",
        "expect_refuse": False,
        "must_article": "93",
        "must_filename": "nk.txt",
        "must_contain": "добавленную",
    },
    {
        "id": "hit-xref-trap",
        "kind": "article_number",
        "question": "ст. 625 ГК срок регистрации аренды",
        "expect_refuse": False,
        "must_article": "625",
        "must_filename": "gk.txt",
        "must_not_contain": "репатриация",
    },
    {
        "id": "miss-article-999",
        "kind": "absent",
        "question": "Что говорит статья 999 ГК?",
        "expect_refuse": True,
    },
    {
        "id": "miss-ifrs-16",
        "kind": "absent",
        "question": "Какой срок лизинга по МСФО 16 для нерезидента?",
        "expect_refuse": True,
    },
    {
        "id": "miss-koap",
        "kind": "absent",
        "question": "Какой штраф по КоАП за парковку на газоне?",
        "expect_refuse": True,
    },
    {
        "id": "miss-refinance-2010",
        "kind": "absent",
        "question": "Какая ставка рефинансирования НБРБ на 1 января 2010 года?",
        "expect_refuse": True,
    },
    {
        "id": "miss-nk-250",
        "kind": "absent",
        "question": "статья 250 Налогового кодекса про трансфертное ценообразование",
        "expect_refuse": True,
    },
    {
        "id": "miss-rf-ooo",
        "kind": "absent",
        "question": "Что говорит закон РФ об обществах с ограниченной ответственностью?",
        "expect_refuse": True,
    },
    {
        "id": "miss-instr-999",
        "kind": "absent",
        "question": "пункт 88.1 инструкции 999 Национального банка про криптовалюту",
        "expect_refuse": True,
    },
    {
        "id": "miss-client-excel",
        "kind": "absent",
        "question": "Сколько начислено в выгрузке 1С по счёту 76?",
        "expect_refuse": True,
    },
    {
        "id": "miss-ifrs-9",
        "kind": "absent",
        "question": "Как классифицировать финансовый актив по IFRS 9?",
        "expect_refuse": True,
    },
]


def _article_in(text: str, num: str) -> bool:
    blob = text or ""
    return f"Статья {num}" in blob or f"статья {num}" in blob or f"## Статья {num}" in blob


async def bag_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic stand-in for qwen3-embedding: hashed bag-of-tokens."""
    dim = 48
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        h = 2166136261
        for tok in (text or "").lower().split():
            for ch in tok:
                h ^= ord(ch)
                h = (h * 16777619) & 0xFFFFFFFF
            vec[h % dim] += 1.0
        out.append(vec)
    return out


async def run_eval(cases: list[dict] | None = None) -> list[dict]:
    from app.services.knowledge_retrieve import retrieve_for_ask

    rows = []
    for spec in cases or GOLD:
        picked = await retrieve_for_ask(
            [dict(ch) for ch in CORPUS],
            spec["question"],
            top_k=4,
            embed_fn=bag_embed,
        )
        blob = " ".join((p.get("text") or "") for p in picked)
        refused = not picked
        ok = refused == spec["expect_refuse"]
        errors: list[str] = []
        if spec["expect_refuse"]:
            if picked:
                ok = False
                errors.append("expected refuse")
        else:
            if not picked:
                ok = False
                errors.append("unexpected refuse")
            must_art = spec.get("must_article")
            if must_art and not _article_in(blob, must_art):
                ok = False
                errors.append(f"missing article {must_art}")
            must_fn = spec.get("must_filename")
            if must_fn and not any(p.get("filename") == must_fn for p in picked):
                ok = False
                errors.append(f"missing file {must_fn}")
            needle = spec.get("must_contain")
            if needle and needle.lower() not in blob.lower():
                ok = False
                errors.append(f"missing text {needle!r}")
            banned = spec.get("must_not_contain")
            if banned and banned.lower() in blob.lower():
                ok = False
                errors.append(f"forbidden text {banned!r}")
        rows.append(
            {
                "id": spec["id"],
                "kind": spec["kind"],
                "ok": ok,
                "refused": refused,
                "errors": errors,
                "files": [p.get("filename") for p in picked],
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline RAG retrieval eval")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args()
    import asyncio

    rows = asyncio.run(run_eval())
    passed = sum(1 for r in rows if r["ok"])
    if args.json:
        print(json.dumps({"passed": passed, "total": len(rows), "rows": rows}, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            mark = "PASS" if row["ok"] else "FAIL"
            extra = f" {row['errors']}" if row["errors"] else ""
            print(f"{mark:4}  {row['id']:28} refuse={str(row['refused']).lower():5}{extra}")
        print(f"{passed}/{len(rows)} passed")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
