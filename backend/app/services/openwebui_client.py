from __future__ import annotations

from pathlib import Path

import httpx

from app.config import settings


class OpenWebUIError(RuntimeError):
    pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


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
        return out


async def ensure_collection(name: str, description: str, api_key: str) -> dict:
    url = settings.openwebui_url.rstrip("/")
    async with httpx.AsyncClient(timeout=60.0) as client:
        listing = await client.get(f"{url}/api/v1/knowledge/", headers=_headers(api_key))
        if listing.status_code == 401:
            raise OpenWebUIError("Open WebUI: неверный API ключ")
        listing.raise_for_status()
        items = listing.json() or []
        for item in items:
            if (item.get("name") or "") == name:
                return item
        created = await client.post(
            f"{url}/api/v1/knowledge/create",
            headers=_headers(api_key),
            json={"name": name, "description": description, "access_grants": []},
        )
        if created.status_code >= 400:
            raise OpenWebUIError(f"Create knowledge failed: {created.text[:400]}")
        return created.json()


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
            raise OpenWebUIError(f"Upload {path.name} failed: {resp.text[:400]}")
        return resp.json()


async def add_file_to_knowledge(knowledge_id: str, file_id: str, api_key: str) -> dict:
    url = settings.openwebui_url.rstrip("/")
    async with httpx.AsyncClient(timeout=180.0) as client:
        resp = await client.post(
            f"{url}/api/v1/knowledge/{knowledge_id}/file/add",
            headers=_headers(api_key),
            json={"file_id": file_id},
        )
        if resp.status_code >= 400:
            raise OpenWebUIError(f"Add file failed: {resp.text[:400]}")
        return resp.json()
