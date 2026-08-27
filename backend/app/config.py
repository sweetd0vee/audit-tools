from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Local LLM (Ollama)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.8:27b"
    ollama_timeout_sec: float = 180.0
    ollama_num_ctx: int = 32768

    # SearXNG (self-hosted)
    searxng_url: str = "http://localhost:8080/search"
    searxng_timeout_sec: float = 30.0

    # Off: SearXNG + official site search only. On: also DuckDuckGo HTML + Bing
    # (act title leaves the contour). Recall vs locality — not both.
    npa_web_fallback: bool = False

    # Browser origins allowed to call the API. Loopback only by default.
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if not text or text.startswith("["):
                return value
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    # Only these domains may be searched / downloaded
    domain_allowlist: list[str] = [
        "pravo.gov.by",
        "pravo.by",
        "etalonline.by",
        "nbrb.by",
        "minfin.gov.by",
        "nalog.gov.by",
        "government.by",
        "president.gov.by",
    ]

    # Storage
    data_root: Path = Path(__file__).resolve().parent.parent / "data" / "audit_cases"
    max_docs_to_propose: int = 15
    max_search_results_per_query: int = 5
    download_timeout_sec: float = 45.0

    # Knowledge / RAG
    ollama_embed_model: str = "qwen3-embedding:latest"
    # Query-time cross-encoder. Empty = skip. Official qwen3-reranker is not on
    # Ollama library; 4B Q8 is the best local size in this family (~4.3 GB).
    ollama_rerank_model: str = "dengcao/Qwen3-Reranker-4B:Q8_0"
    rerank_timeout_sec: float = 60.0
    rag_rerank_candidates: int = 24
    rag_candidates: int = 40
    rag_neighbor: int = 1
    rag_mmr_lambda: float = 0.7
    # Below this P(yes) the chunk is not evidence. Empty after the gate → refuse.
    rag_min_rerank: float = 0.30
    # IDF-weighted lexical floor when the reranker is down (one distinctive term ≈ 1).
    rag_min_lexical: float = 0.8
    chunk_size: int = 1600
    chunk_overlap: int = 180
    summary_max_chars: int = 14000
    # Short acts still go one-shot. Longer acts: query-focused RAG (hybrid + RRF + MMR).
    summary_section_chars: int = 10000
    summary_section_overlap: int = 300
    summary_timeout_sec: float = 420.0
    summary_rag_top_k: int = 16
    summary_rag_candidates: int = 80
    summary_rag_neighbor: int = 1
    summary_rag_mmr_lambda: float = 0.7
    rag_top_k: int = 16
    embed_timeout_sec: float = 120.0
    brief_chars_per_page: int = 1800
    brief_timeout_sec: float = 1800.0

    # Open WebUI (optional sync)
    openwebui_url: str = "http://localhost:3000"
    openwebui_api_key: str = ""

    # LLM prompts (docs/prompts/*.txt). Compose: /app/prompts
    prompts_dir: Optional[Path] = None

    # Observability
    log_level: str = "INFO"

    # Upload into the case knowledge library (NPA text, not client Excel)
    max_upload_bytes: int = 32 * 1024 * 1024


settings = Settings()
