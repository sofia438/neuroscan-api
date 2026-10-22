from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app import models  # noqa: F401 - register model metadata
from app.api import auth, chat
from app.core.config import get_settings
from app.db import Base, database_configured, dispose_engine, get_engine


@asynccontextmanager
async def lifespan(_: FastAPI):
    if database_configured():
        if len(settings.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must be set to a random value with at least 32 characters")
        Base.metadata.create_all(bind=get_engine())
        # Add the image URL field when upgrading an existing first-version database.
        with get_engine().begin() as connection:
            connection.execute(text("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS image_urls JSON NOT NULL DEFAULT '[]'"))
            connection.execute(text("ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS image_data JSON NOT NULL DEFAULT '[]'"))
    yield
    dispose_engine()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])
app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {"name": settings.app_name, "playground": "/playground", "docs": "/docs", "health": "/health"}


@app.get("/playground", response_class=HTMLResponse)
def playground() -> HTMLResponse:
    page = Path(__file__).parent / "app" / "static" / "playground.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ready"})
