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


def upsert_pipe(
    *,
    base: str,
    token: str,
    content: str,
    audit_api: str,
    public_api: str,
    owui_key: str = "",
) -> None:
    form = {
        "id": PIPE_ID,
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
    root = base.rstrip("/")
    list_status, listing = _json_request("GET", f"{root}/api/v1/functions/", token)
    if list_status in (401, 403):
        raise SystemExit(
            f"Open WebUI отклонил ключ ({list_status}). "
            "Проверьте OPENWEBUI_API_KEY (админский API key)."
        )
    if list_status >= 400:
        raise SystemExit(f"GET /functions → {list_status}: {listing}")

    existing_ids: set[str] = set()
    rows: list = listing if isinstance(listing, list) else []
    if isinstance(listing, dict):
        maybe = listing.get("items") or listing.get("functions") or []
        rows = list(maybe.values()) if isinstance(maybe, dict) else maybe
    listed_active = False
    for row in rows:
        if isinstance(row, dict) and row.get("id"):
            existing_ids.add(str(row["id"]))
            if str(row["id"]) == PIPE_ID:
                listed_active = bool(row.get("is_active"))

    if PIPE_ID in existing_ids:
        up_status, up_body = _json_request(
            "POST", f"{root}/api/v1/functions/id/{PIPE_ID}/update", token, form
        )
        if up_status >= 400:
            raise SystemExit(f"Обновление Pipe не удалось ({up_status}): {up_body}")
        print(f"Pipe `{PIPE_ID}` обновлён из git.")
        info = up_body if isinstance(up_body, dict) else {}
        is_active = info["is_active"] if "is_active" in info else listed_active
    else:
        cr_status, cr_body = _json_request(
            "POST", f"{root}/api/v1/functions/create", token, form
        )
        if cr_status >= 400:
            raise SystemExit(f"Создание Pipe не удалось ({cr_status}): {cr_body}")
        print(f"Pipe `{PIPE_ID}` создан.")
        info = cr_body if isinstance(cr_body, dict) else {}
        is_active = bool(info.get("is_active"))

    if not is_active:
        tog_status, tog_body = _json_request(
            "POST", f"{root}/api/v1/functions/id/{PIPE_ID}/toggle", token
        )
        if tog_status >= 400:
            raise SystemExit(f"Включить Pipe не удалось ({tog_status}): {tog_body}")
        print(f"Pipe `{PIPE_ID}` включён.")

    valves = {
        "AUDIT_API": audit_api,
        "PUBLIC_API": public_api,
        "TIMEOUT_SEC": 600,
        "BRIEF_TIMEOUT_SEC": 1800,
    }
    if owui_key:
        valves["OPENWEBUI_API_KEY"] = owui_key
    v_status, v_body = _json_request(
        "POST",
        f"{root}/api/v1/functions/id/{PIPE_ID}/valves/update",
        token,
        valves,
    )
    if v_status >= 400:
        raise SystemExit(f"Valves Pipe не записались ({v_status}): {v_body}")
    print(
        f"Valves: AUDIT_API={audit_api} PUBLIC_API={public_api}"
    )


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
