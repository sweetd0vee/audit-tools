from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from app.clock import utc_now


def new_id() -> str:
    return uuid4().hex[:12]


class CaseStatus(str, Enum):
    created = "created"
    proposed = "proposed"
    selected = "selected"
    downloading = "downloading"
    ready = "ready"
    failed = "failed"


class ProposedDocument(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    doc_type: str = Field(description="закон / кодекс / инструкция / постановление / указ / иное")
    why_needed: str
    search_queries: list[str] = Field(default_factory=list)
    priority: int = Field(default=2, ge=1, le=3, description="1=обязательно, 2=желательно, 3=опционально")
    selected: bool = False
    # Filled after search/download
    found_url: Optional[str] = None
    local_path: Optional[str] = None
    download_status: Optional[str] = None
    download_error: Optional[str] = None


class KnowledgeItem(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str
    source: str = "downloaded"
    filename: str
    local_path: str
    text_path: Optional[str] = None
    origin_document_id: Optional[str] = None
    bytes: int = 0
    char_count: int = 0
    chunk_count: int = 0
    extract_status: Optional[str] = None
    extract_error: Optional[str] = None
    summary: Optional[str] = None
    summary_status: Optional[str] = None
    summary_error: Optional[str] = None
    summary_path: Optional[str] = None
    citations: list[dict[str, Any]] = Field(default_factory=list)


class CreateCaseRequest(BaseModel):
    inspection_name: str = Field(min_length=3, description="Название проверки")
    keywords: list[str] = Field(default_factory=list, description="Ключевые термины")
    notes: Optional[str] = None


class CreateCaseResponse(BaseModel):
    case_id: str
    status: CaseStatus
    inspection_name: str
    keywords: list[str]
    created_at: datetime


class ProposeResponse(BaseModel):
    case_id: str
    status: CaseStatus
    documents: list[ProposedDocument]
    model: str
    raw_topics: list[str] = Field(default_factory=list)
    raw_response: Optional[str] = None
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    elapsed_ms: Optional[int] = None


class SelectDocumentsRequest(BaseModel):
    document_ids: list[str] = Field(
        default_factory=list,
        description="ID документов из propose, которые аудитор утвердил",
    )
    extra_titles: list[str] = Field(
        default_factory=list,
        description="Названия актов, которых нет в списке: сервер сам найдёт и скачает",
    )
    manual_urls: dict[str, str] = Field(
        default_factory=dict,
        description="Опционально: document_id -> URL, если знаете точную ссылку",
    )


class SelectDocumentsResponse(BaseModel):
    case_id: str
    status: CaseStatus
    selected_count: int
    documents: list[ProposedDocument]


class DownloadResponse(BaseModel):
    case_id: str
    status: CaseStatus
    downloaded: int
    failed: int
    library_dir: str
    archive_name: Optional[str] = None
    archive_url: Optional[str] = None
    documents: list[ProposedDocument]


class CaseState(BaseModel):
    case_id: str
    status: CaseStatus = CaseStatus.created
    inspection_name: str
    keywords: list[str] = Field(default_factory=list)
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    topics: list[str] = Field(default_factory=list)
    documents: list[ProposedDocument] = Field(default_factory=list)
    knowledge: list[KnowledgeItem] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    question: str = Field(min_length=3)
    top_k: Optional[int] = None


class AskResponse(BaseModel):
    answer: str
    sources: list[dict[str, Any]] = Field(default_factory=list)
    model: str
    used_embeddings: bool = False
    used_reranker: bool = False
    used_summaries: bool = False
    refused: bool = False
    refuse_reason: Optional[str] = None


class ChatMessage(BaseModel):
    role: str = Field(description="system | user | assistant")
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    system: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    model: str


class OpenWebUISyncRequest(BaseModel):
    api_key: Optional[str] = None


class BriefRequest(BaseModel):
    force: bool = False
    items: Optional[str] = None
    items_min: Optional[int] = Field(default=None, ge=3, le=20)
    items_max: Optional[int] = Field(default=None, ge=3, le=20)
    font: Optional[str] = None


class SelectHypothesesRequest(BaseModel):
    numbers: list[int] = Field(default_factory=list)
    all_high: bool = False
    all_rows: bool = False


class CaseSummary(BaseModel):
    case_id: str
    status: CaseStatus
    inspection_name: str
    keywords: list[str]
    created_at: datetime
    documents_total: int
    documents_selected: int
    documents_downloaded: int
