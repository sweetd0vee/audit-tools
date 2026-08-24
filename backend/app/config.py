from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Local LLM (Ollama)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3.6:35b"
    ollama_timeout_sec: float = 180.0
    ollama_num_ctx: int = 32768

    # SearXNG (self-hosted)
    searxng_url: str = "http://localhost:8080/search"
    searxng_timeout_sec: float = 30.0

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
    chunk_size: int = 1400
    chunk_overlap: int = 180
    summary_max_chars: int = 14000
    summary_section_chars: int = 28000
    summary_section_overlap: int = 600
    summary_timeout_sec: float = 420.0
    rag_top_k: int = 8
    embed_timeout_sec: float = 120.0
    brief_chars_per_page: int = 1800
    brief_timeout_sec: float = 1800.0

    # Open WebUI (optional sync)
    openwebui_url: str = "http://localhost:3000"
    openwebui_api_key: str = ""


settings = Settings()
