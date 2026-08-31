"""Склеить intent.py + audit_agent.py и засеять Pipe в Open WebUI.

Локально:
    python seed/openwebui/seed_pipe.py
    python seed/openwebui/seed_pipe.py --print   # paste для Admin → Functions

Compose: сервис pipe-seed, OPENWEBUI_API_KEY из корневого .env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FUNCTIONS_DIR = HERE / "functions"
PIPE_ID = "auditor"
PIPE_NAME = "Аудитор"
# Старый Function ID: в чате Open WebUI рисует name из этой записи.
# Если функцию создали как `npa`, подпись остаётся «npa», пока не обновим name.
LEGACY_PIPE_IDS = ("npa",)
INTENT_START = "# INTENT_INLINE_START"
INTENT_END = "# INTENT_INLINE_END"


def _intent_body(source: str) -> str:
    lines = source.splitlines()
    i = 0
    if lines and lines[0].lstrip().startswith('"""'):
        if lines[0].count('"""') >= 2:
            i = 1
        else:
            i = 1
            while i < len(lines) and '"""' not in lines[i]:
                i += 1
            i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if (
            not stripped
            or stripped.startswith("from ")
            or stripped.startswith("import ")
        ):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip() + "\n"


def build_pipe_source(
    functions_dir: Path | None = None,
) -> str:
    root = functions_dir or FUNCTIONS_DIR
    pipe = (root / "audit_agent.py").read_text(encoding="utf-8")
    intent = (root / "intent.py").read_text(encoding="utf-8")
    start = pipe.index(INTENT_START)
    end = pipe.index(INTENT_END) + len(INTENT_END)
    return pipe[:start] + INTENT_START + "\n" + _intent_body(intent) + INTENT_END + pipe[end:]


def _json_request(
    method: str,
    url: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 60.0,
) -> tuple[int, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            body: object = json.loads(raw) if raw else {}
            return resp.status, body
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw) if raw else {"detail": exc.reason}
        except json.JSONDecodeError:
            body = {"detail": raw.decode("utf-8", errors="replace")[:400]}
        return exc.code, body


def wait_openwebui(base: str, attempts: int = 60, delay: float = 2.0) -> None:
    last = ""
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(base.rstrip("/") + "/", timeout=5) as resp:
                if resp.status < 500:
                    return
                last = f"HTTP {resp.status}"
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
        time.sleep(delay)
    raise SystemExit(f"Open WebUI не отвечает на {base}: {last}")


def _function_rows(listing: object) -> list[dict]:
    rows: list = listing if isinstance(listing, list) else []
    if isinstance(listing, dict):
        maybe = listing.get("items") or listing.get("functions") or []
        rows = list(maybe.values()) if isinstance(maybe, dict) else maybe
    return [row for row in rows if isinstance(row, dict)]


def target_function_ids(existing_ids: set[str]) -> list[str]:
    """Обновить все уже существующие Pipe (`auditor` и legacy `npa`), иначе создать `auditor`.

    Старые чаты часто привязаны к id `npa`. Если трогать только `auditor`,
    в открытом чате модель пропадает («Model not selected»).
    """
    ids: list[str] = []
    if PIPE_ID in existing_ids:
        ids.append(PIPE_ID)
    for legacy in LEGACY_PIPE_IDS:
        if legacy in existing_ids and legacy not in ids:
            ids.append(legacy)
    return ids or [PIPE_ID]


def _listed_functions(listing: object) -> tuple[set[str], dict[str, bool]]:
    existing_ids: set[str] = set()
    active_by_id: dict[str, bool] = {}
    for row in _function_rows(listing):
        if not row.get("id"):
            continue
        fid = str(row["id"])
        existing_ids.add(fid)
        active_by_id[fid] = bool(row.get("is_active"))
    return existing_ids, active_by_id


def _ensure_active(root: str, token: str, function_id: str) -> None:
    status, listing = _json_request("GET", f"{root}/api/v1/functions/", token)
    if status >= 400:
        print(f"Не проверил Pipe `{function_id}` ({status}): {listing}")
        return
    _, active_by_id = _listed_functions(listing)
    if active_by_id.get(function_id):
        return
    tog_status, tog_body = _json_request(
        "POST", f"{root}/api/v1/functions/id/{function_id}/toggle", token
    )
    if tog_status >= 400:
        raise SystemExit(
            f"Включить Pipe `{function_id}` не удалось ({tog_status}): {tog_body}"
        )
    print(f"Pipe `{function_id}` включён.")


def _pipe_form(function_id: str, content: str) -> dict:
    return {
        "id": function_id,
        "name": PIPE_NAME,
        "content": content,
        "meta": {
            "description": (
                "Агент проверки. Документы, саммари, total, программа, "
                "гипотезы, мнение, заключение."
            ),
            "manifest": {},
        },
    }


def _upsert_function(
    *,
    root: str,
    token: str,
    function_id: str,
    content: str,
    existing_ids: set[str],
    create: bool,
) -> None:
    form = _pipe_form(function_id, content)
    if function_id in existing_ids:
        up_status, up_body = _json_request(
            "POST", f"{root}/api/v1/functions/id/{function_id}/update", token, form
        )
        if up_status >= 400:
            raise SystemExit(
                f"Обновление Pipe `{function_id}` не удалось ({up_status}): {up_body}"
            )
        print(f"Pipe `{function_id}` обновлён, имя «{PIPE_NAME}».")
    elif create:
        cr_status, cr_body = _json_request(
            "POST", f"{root}/api/v1/functions/create", token, form
        )
        if cr_status >= 400:
            raise SystemExit(f"Создание Pipe не удалось ({cr_status}): {cr_body}")
        print(f"Pipe `{function_id}` создан.")
    else:
        return
    time.sleep(1)
    _ensure_active(root, token, function_id)


def _rename_workspace_models(root: str, token: str) -> None:
    status, listing = _json_request("GET", f"{root}/api/v1/models/", token)
    if status >= 400:
        return
    rows = listing if isinstance(listing, list) else []
    if isinstance(listing, dict):
        maybe = listing.get("items") or listing.get("models") or listing.get("data") or []
        rows = list(maybe.values()) if isinstance(maybe, dict) else maybe
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "")
        name = str(row.get("name") or "")
        if model_id not in LEGACY_PIPE_IDS and name not in LEGACY_PIPE_IDS:
            continue
        if name == PIPE_NAME:
            continue
        payload = dict(row)
        payload["name"] = PIPE_NAME
        up_status, up_body = _json_request(
            "POST",
            f"{root}/api/v1/models/model/update?id={model_id}",
            token,
            payload,
        )
        if up_status >= 400:
            print(f"Модель `{model_id}` не переименовалась ({up_status}): {up_body}")
        else:
            print(f"Модель `{model_id}` переименована в «{PIPE_NAME}».")


def upsert_pipe(
    *,
    base: str,
    token: str,
    content: str,
    audit_api: str,
    public_api: str,
    owui_key: str = "",
) -> None:
    root = base.rstrip("/")
    list_status, listing = _json_request("GET", f"{root}/api/v1/functions/", token)
    if list_status in (401, 403):
        raise SystemExit(
            f"Open WebUI отклонил ключ ({list_status}). "
            "Проверьте OPENWEBUI_API_KEY (админский API key)."
        )
    if list_status >= 400:
        raise SystemExit(f"GET /functions → {list_status}: {listing}")

    existing_ids, _active_by_id = _listed_functions(listing)

    for function_id in target_function_ids(existing_ids):
        _upsert_function(
            root=root,
            token=token,
            function_id=function_id,
            content=content,
            existing_ids=existing_ids,
            create=True,
        )

    valves = {
        "AUDIT_API": audit_api,
        "PUBLIC_API": public_api,
        "TIMEOUT_SEC": 600,
        "BRIEF_TIMEOUT_SEC": 1800,
    }
    if owui_key:
        valves["OPENWEBUI_API_KEY"] = owui_key
    for function_id in target_function_ids(existing_ids):
        v_status, v_body = _json_request(
            "POST",
            f"{root}/api/v1/functions/id/{function_id}/valves/update",
            token,
            valves,
        )
        if v_status >= 400:
            raise SystemExit(
                f"Valves Pipe `{function_id}` не записались ({v_status}): {v_body}"
            )
    print(f"Valves: AUDIT_API={audit_api} PUBLIC_API={public_api}")
    for function_id in target_function_ids(existing_ids):
        _ensure_active(root, token, function_id)
    _rename_workspace_models(root, token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Засев Pipe «Аудитор» в Open WebUI")
    parser.add_argument(
        "--print",
        dest="print_source",
        action="store_true",
        help="Только склеить paste в stdout, без API",
    )
    parser.add_argument("--openwebui-url", default=os.environ.get("OPENWEBUI_URL", "http://localhost:3000"))
    parser.add_argument("--api-key", default=os.environ.get("OPENWEBUI_API_KEY", ""))
    parser.add_argument(
        "--audit-api",
        default=os.environ.get("PIPE_AUDIT_API", "http://backend:8100"),
    )
    parser.add_argument(
        "--public-api",
        default=os.environ.get("PIPE_PUBLIC_API", "http://localhost:8100"),
    )
    args = parser.parse_args(argv)

    source = build_pipe_source()
    compile(source, "audit_agent.paste.py", "exec")

    if args.print_source:
        sys.stdout.write(source)
        return 0

    token = (args.api_key or "").strip()
    if not token:
        print(
            "Pipe не засеян: задайте OPENWEBUI_API_KEY "
            "(Open WebUI → Настройки → Аккаунт → API Keys).",
            file=sys.stderr,
        )
        return 0

    wait_openwebui(args.openwebui_url)
    last_error = ""
    for attempt in range(1, 31):
        try:
            upsert_pipe(
                base=args.openwebui_url,
                token=token,
                content=source,
                audit_api=args.audit_api,
                public_api=args.public_api,
                owui_key=token,
            )
            return 0
        except SystemExit as exc:
            last_error = str(exc)
            print(f"попытка {attempt}/30: {last_error}", file=sys.stderr)
            time.sleep(min(15, 2 * attempt))
    print(last_error, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
