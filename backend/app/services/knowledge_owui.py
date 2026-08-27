from __future__ import annotations

import logging
from pathlib import Path

from app.clock import utc_now
from app.config import settings
from app.filenames import safe_stem
from app.prompts import prompt
from app.services.knowledge_ingest import text_dir
from app.services.openwebui_client import (
    OpenWebUIError,
    add_file_to_knowledge,
    ensure_collection,
    ping,
    upload_file,
)
from app.storage import store

logger = logging.getLogger(__name__)


def export_pack_files(case_id: str) -> list[tuple[str, bytes]]:
    """Files for Open WebUI / zip pack: clean texts + summaries + howto."""
    state = store.get(case_id)
    files: list[tuple[str, bytes]] = []
    for item in state.knowledge:
        if item.text_path and Path(item.text_path).exists():
            name = f"docs/{safe_stem(item.filename)}.txt"
            files.append((name, Path(item.text_path).read_bytes()))
        if item.summary:
            name = f"summaries/{safe_stem(item.filename)}.md"
            body = f"# {item.title}\n\n{item.summary}\n"
            files.append((name, body.encode("utf-8")))

    howto = f"""# Как подключить базу НПА в Open WebUI

Коллекция: {state.inspection_name}
Кейс: {case_id}

1. Откройте http://localhost:3000
2. Workspace → Knowledge → New Knowledge
3. Имя: «{state.inspection_name}»
4. Загрузите файлы из папки docs/ (чистый текст НПА)
5. По желанию добавьте summaries/
6. В чате с моделью нажмите # и выберите эту коллекцию
7. Задавайте вопросы по нормам. Модель получит фрагменты из базы + свои знания.

Системный промпт (вставьте в модель / чат):
{prompt("ask_system")}
"""
    files.append(("README_OPENWEBUI.md", howto.encode("utf-8")))
    return files


async def sync_openwebui(case_id: str, api_key: str | None = None) -> dict:
    key = (api_key or settings.openwebui_api_key or "").strip()
    if not key:
        raise OpenWebUIError("Нет API ключа Open WebUI. Укажите ключ или скачайте пакет вручную.")
    state = store.get(case_id)
    name = f"Аудит: {state.inspection_name}"[:80]
    desc = f"НПА кейса {case_id}. Ключевые слова: {', '.join(state.keywords)}"
    collection = await ensure_collection(name, desc, key)
    kid = collection.get("id") if isinstance(collection, dict) else collection
    if not kid:
        raise OpenWebUIError("Open WebUI не вернул id коллекции")
    kid = str(kid)

    uploaded = []
    extracted = text_dir(case_id)
    paths = sorted(extracted.glob("*.txt"))
    if not paths:
        raise OpenWebUIError("Нет .txt для отправки. Сначала соберите базу знаний.")

    for path in paths:
        info = await upload_file(path, key)
        fid = info.get("id") if isinstance(info, dict) else None
        if not fid:
            uploaded.append({"file": path.name, "status": "error", "error": "нет file id"})
            continue
        try:
            await add_file_to_knowledge(kid, str(fid), key)
            uploaded.append({"file": path.name, "file_id": fid, "status": "ok"})
        except OpenWebUIError as exc:
            uploaded.append({"file": path.name, "file_id": fid, "status": "error", "error": str(exc)})

    state.meta["openwebui_knowledge_id"] = kid
    state.meta["openwebui_knowledge_name"] = name
    store.save(state)
    try:
        store.append_jsonl(
            case_id,
            "trail/openwebui.jsonl",
            {
                "ts": utc_now().isoformat(),
                "case_id": case_id,
                "knowledge_id": kid,
                "uploaded": uploaded,
            },
        )
    except Exception:
        logger.warning("openwebui trail write failed case=%s", case_id, exc_info=True)
    return {
        "knowledge_id": kid,
        "name": name,
        "uploaded": uploaded,
        "url": f"{settings.openwebui_url.rstrip('/')}/workspace/knowledge",
    }


async def openwebui_status(api_key: str | None = None) -> dict:
    try:
        return await ping(api_key)
    except Exception as exc:  # noqa: BLE001
        return {
            "url": settings.openwebui_url,
            "reachable": False,
            "auth": False,
            "error": str(exc),
        }
