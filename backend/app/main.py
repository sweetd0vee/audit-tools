from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import settings
from app.logging_setup import RequestLogMiddleware, configure_logging
from app.routers.knowledge import router as knowledge_router
from app.routers.library import router as library_router
from app.services.ollama_client import close_clients

configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await close_clients()


app = FastAPI(
    title="Audit Tools API",
    description="Propose NPA → download → summaries + RAG knowledge base",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(library_router)
app.include_router(knowledge_router)


@app.get("/")
def root():
    return {
        "service": "audit-tools",
        "version": __version__,
        "docs": "/docs",
        "ollama_model": settings.ollama_model,
        "embed_model": settings.ollama_embed_model,
        "rerank_model": settings.ollama_rerank_model,
        "searxng_url": settings.searxng_url,
        "data_root": str(settings.data_root),
    }


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}
