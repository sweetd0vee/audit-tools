from __future__ import annotations

import os
from pathlib import Path

from app.config import settings


def prompts_dir() -> Path:
    configured = getattr(settings, "prompts_dir", None)
    if configured:
        return Path(configured)
    env = (os.environ.get("PROMPTS_DIR") or "").strip()
    if env:
        return Path(env)
    docker = Path("/app/prompts")
    if (docker / "summary_system.txt").is_file():
        return docker
    return Path(__file__).resolve().parents[2] / "docs" / "prompts"


def load(name: str) -> str:
    path = prompts_dir() / f"{name}.txt"
    try:
        return path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Нет промпта {name!r} ({path}). Положите файл в docs/prompts/."
        ) from exc


def fill(template: str, **kwargs: object) -> str:
    """Replace {name} placeholders. Values are not scanned for further placeholders."""
    sentinels: dict[str, str] = {}
    text = template
    for i, key in enumerate(sorted(kwargs, key=len, reverse=True)):
        token = f"\x00PROMPT{i}\x00"
        sentinels[token] = str(kwargs[key])
        text = text.replace("{" + key + "}", token)
    for token, value in sentinels.items():
        text = text.replace(token, value)
    return text


def prompt(name: str, **kwargs: object) -> str:
    text = load(name)
    return fill(text, **kwargs) if kwargs else text
