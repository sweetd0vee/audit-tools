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


def _as_items(payload: Any) -> list[dict]:
    """Open WebUI used to return a bare list; current versions return {items, total}."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        items = payload.get("items")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        if payload.get("id") or payload.get("name"):
            return [payload]
    return []


def _id_of(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("id")
        return str(value) if value else None
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
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
        payload = listing.json()
        batch = _as_items(payload)
        items.extend(batch)
        if isinstance(payload, dict) and "total" in payload:
            total = int(payload.get("total") or 0)
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
        payload = created.json()
        if isinstance(payload, dict) and (payload.get("id") or payload.get("name")):
            return payload
        kid = _id_of(payload)
        if kid:
            return {"id": kid, "name": name}
        raise OpenWebUIError(f"Create knowledge: unexpected response {str(payload)[:300]}")


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
        if resp.status_code == 404:
            if saw_endpoint:
                raise OpenWebUIError(f"File {file_id} disappeared while processing")
            return
        if resp.status_code >= 400:
            return
        saw_endpoint = True
        data = resp.json() if resp.content else {}
        status = data.get("status") if isinstance(data, dict) else None
        if status == "completed":
            return
        if status == "failed":
            err = data.get("error") if isinstance(data, dict) else None
            raise OpenWebUIError(f"Open WebUI не обработал файл: {err or 'failed'}")
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
        payload = resp.json()
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
        payload = resp.json()
        return payload if isinstance(payload, dict) else {"ok": True}
