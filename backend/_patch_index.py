from pathlib import Path

p = Path(__file__).with_name("app").joinpath("routers", "knowledge.py")
t = p.read_text(encoding="utf-8")
old = """    ingest_library,
    openwebui_status,
    sync_openwebui,
)"""
new = """    ingest_library,
    openwebui_status,
    rebuild_index,
    sync_openwebui,
)"""
if old not in t:
    raise SystemExit("import block not found")
t = t.replace(old, new, 1)
needle = '''@router.post("/cases/{case_id}/knowledge/ingest")
def ingest(case_id: str):
    try:
        state = ingest_library(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"case_id": case_id, "items": [k.model_dump() for k in state.knowledge]}
'''
insert = needle + '''

@router.post("/cases/{case_id}/knowledge/index")
def index_knowledge(case_id: str):
    """Collect chunks from downloaded txt without summaries or Open WebUI."""
    try:
        store.get(case_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    payload = rebuild_index(case_id)
    state = store.get(case_id)
    return {
        "case_id": case_id,
        "chunks": len(payload.get("chunks") or []),
        "items": [k.model_dump() for k in state.knowledge],
    }
'''
if needle not in t:
    raise SystemExit("ingest block not found")
t = t.replace(needle, insert, 1)
p.write_text(t, encoding="utf-8")
print("patched", p)
