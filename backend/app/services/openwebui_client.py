from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from app.config import settings


class OpenWebUIError(RuntimeError):
    pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _json(resp: httpx.Response) -> Any:
    if not resp.content:
        return None
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise OpenWebUIError(
            f"Open WebUI вернул не JSON ({resp.status_code}): {(resp.text or '')[:200]}"
        ) from exc


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return (resp.text or "")[:400]
    detail = data.get("detail") if isinstance(data, dict) else data
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                parts.append(str(item.get("msg") or item))
            else:
                parts.append(str(item))
        return "; ".join(parts)[:400]
    return str(detail or resp.text)[:400]


def _dicts_from(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        if value.get("id") or value.get("name"):
            return [value]
        return [x for x in value.values() if isinstance(x, dict)]
    return []


def _as_items(payload: Any) -> list[dict]:
    """Open WebUI used to return a bare list; current versions return {items, total}.

    Iterating the wrapper dict yields string keys ('items', 'total'); callers must
    not do `for item in payload: item.get(...)`.
    """
    if isinstance(payload, list):
        return _dicts_from(payload)
    if isinstance(payload, dict):
        for key in ("items", "knowledge_bases"):
            if key in payload:
                return _dicts_from(payload.get(key))
        return _dicts_from(payload)
    return []


def _id_of(payload: Any) -> str | None:
    if isinstance(payload, list) and payload:
        return _id_of(payload[0])
    if isinstance(payload, dict):
        for key in ("id", "knowledge_id", "file_id"):
            value = payload.get(key)
            if value:
                return str(value)
        nested = payload.get("data")
        if isinstance(nested, (dict, list)):
            return _id_of(nested)
        items = payload.get("items")
        if isinstance(items, list) and items:
            return _id_of(items[0])
        return None
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return None


def _status_of(payload: Any) -> str | None:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    data = payload.get("data")
    if isinstance(data, dict):
        nested = data.get("status")
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
        if data.get("content"):
            return "completed"
    return None


async def ping(api_key: str | None = None) -> dict:
    url = settings.openwebui_url.rstrip("/")
    key = (api_key or settings.openwebui_api_key or "").strip()
    async with httpx.AsyncClient(timeout=15.0) as client:
        health = await client.get(f"{url}/")
        out = {"url": url, "reachable": health.status_code < 500, "auth": False}
        if not key:
            return out
        resp = await client.get(f"{url}/api/v1/knowledge/", headers=_headers(key))
        out["auth"] = resp.status_code == 200
        out["status_code"] = resp.status_code
        if resp.status_code != 200:
            out["error"] = _error_text(resp)
        return out


async def _list_knowledge(client: httpx.AsyncClient, url: str, api_key: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page <= 20:
        listing = await client.get(
            f"{url}/api/v1/knowledge/",
            headers=_headers(api_key),
            params={"page": page},
        )
        if listing.status_code == 401:
            raise OpenWebUIError("Open WebUI: неверный API ключ")
        if listing.status_code >= 400:
            raise OpenWebUIError(f"List knowledge failed: {_error_text(listing)}")
        payload = _json(listing)
        batch = _as_items(payload)
        items.extend(batch)
        if isinstance(payload, dict) and "total" in payload:
            try:
                total = int(payload.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if len(items) >= total or not batch:
                break
        else:
            break
        page += 1
    return items


async def ensure_collection(name: str, description: str, api_key: str) -> dict:
    url = settings.openwebui_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60.0) as client:
        items = await _list_knowledge(client, url, api_key)
        for item in items:
            if not isinstance(item, dict):
                continue
            if (item.get("name") or "") == name:
                return item
        created = await client.post(
            f"{url}/api/v1/knowledge/create",
            headers=_headers(api_key),
            json={"name": name, "description": description, "access_grants": []},
        )
        if created.status_code >= 400:
            # older Open WebUI expected access_control instead of access_grants
            created = await client.post(
                f"{url}/api/v1/knowledge/create",
                headers=_headers(api_key),
                json={"name": name, "description": description},
            )
        if created.status_code >= 400:
            raise OpenWebUIError(f"Create knowledge failed: {_error_text(created)}")
        payload = _json(created)
        if isinstance(payload, dict) and (payload.get("id") or payload.get("name")):
            return payload
        kid = _id_of(payload)
        if kid:
            return {"id": kid, "name": name}
        raise OpenWebUIError(f"Create knowledge: unexpected response {str(payload)[:300]}")


async def _file_record_status(
    client: httpx.AsyncClient,
    url: str,
    file_id: str,
    api_key: str,
) -> str | None:
    resp = await client.get(
        f"{url}/api/v1/files/{file_id}",
        headers=_headers(api_key),
    )
    if resp.status_code >= 400:
        return None
    return _status_of(_json(resp))


async def wait_file_processed(
    client: httpx.AsyncClient,
    url: str,
    file_id: str,
    api_key: str,
    timeout_sec: float = 180.0,
) -> None:
    deadline = time.monotonic() + timeout_sec
    saw_endpoint = False
    while time.monotonic() < deadline:
        resp = await client.get(
            f"{url}/api/v1/files/{file_id}/process/status",
            headers=_headers(api_key),
        )
        if resp.status_code == 401:
            raise OpenWebUIError("Open WebUI: неверный API ключ")
        if resp.status_code == 404:
            if saw_endpoint:
                raise OpenWebUIError(f"File {file_id} disappeared while processing")
            # Older Open WebUI has no process/status route.
            return
        if resp.status_code >= 400:
            # Open WebUI 500s when file.data is a string/None and it calls .get().
            status = await _file_record_status(client, url, file_id, api_key)
        else:
            saw_endpoint = True
            status = _status_of(_json(resp))
            if not status:
                status = await _file_record_status(client, url, file_id, api_key)

        if status == "completed":
            return
        if status == "failed":
            raise OpenWebUIError(f"Open WebUI не обработал файл {file_id}")
        await asyncio.sleep(1.5)
    raise OpenWebUIError(f"Таймаут обработки файла {file_id} в Open WebUI")


async def upload_file(path: Path, api_key: str) -> dict:
    url = settings.openwebui_url.rstrip("/")
    async with httpx.AsyncClient(timeout=180.0) as client:
        with path.open("rb") as fh:
            resp = await client.post(
                f"{url}/api/v1/files/",
                headers=_headers(api_key),
                files={"file": (path.name, fh, "text/plain")},
            )
        if resp.status_code >= 400:
            raise OpenWebUIError(f"Upload {path.name} failed: {_error_text(resp)}")
        payload = _json(resp)
        fid = _id_of(payload)
        if not fid:
            raise OpenWebUIError(f"Upload {path.name}: нет id в ответе {str(payload)[:300]}")
        if not isinstance(payload, dict):
            payload = {"id": fid}
        await wait_file_processed(client, url, fid, api_key)
        return payload


async def add_file_to_knowledge(knowledge_id: str, file_id: str, api_key: str) -> dict:
    url = settings.openwebui_url.rstrip("/")
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{url}/api/v1/knowledge/{knowledge_id}/file/add",
            headers=_headers(api_key),
            json={"file_id": file_id},
        )
        if resp.status_code >= 400:
            raise OpenWebUIError(f"Add file failed: {_error_text(resp)}")
        payload = _json(resp)
        return payload if isinstance(payload, dict) else {"ok": True}
