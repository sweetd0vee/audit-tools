from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers.knowledge import router as knowledge_router
from app.routers.library import router as library_router

app = FastAPI(
    title="Audit Tools API",
    description="Propose NPA → download → summaries + RAG knowledge base",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(library_router)
app.include_router(knowledge_router)


@app.get("/")
def root():
    return {
        "service": "audit-tools",
        "step": 2,
        "docs": "/docs",
        "ollama_model": settings.ollama_model,
        "embed_model": settings.ollama_embed_model,
        "rerank_model": settings.ollama_rerank_model,
        "searxng_url": settings.searxng_url,
        "data_root": str(settings.data_root),
    }
