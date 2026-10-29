import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from app.api import auth, cases, chat
from app.core.config import get_settings
from app.core.middleware import MRIUploadSizeLimitMiddleware
from app.db import database_configured, dispose_engine, get_engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    missing = [
        name
        for name, configured in (
            ("DATABASE_URL", database_configured()),
            ("JWT_SECRET", len(settings.jwt_secret) >= 32),
            ("BREVO_API_KEY", bool(settings.brevo_api_key.strip())),
            ("BREVO_SENDER_EMAIL", bool(settings.brevo_sender_email.strip())),
            ("OPENROUTER_API_KEY", bool(settings.openrouter_api_key.strip())),
        )
        if not configured
    ]
    if missing:
        logger.warning(
            "Startup configuration is incomplete; missing: %s", ", ".join(missing)
        )
    else:
        logger.info("Required service configuration is present")
    if not database_configured():
        raise RuntimeError("DATABASE_URL is required; configure Neon PostgreSQL before starting the API")
    if len(settings.jwt_secret) < 32:
        raise RuntimeError("JWT_SECRET must be set to a random value with at least 32 characters")
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("PostgreSQL connection check succeeded")
    except Exception:
        logger.exception("Configured database connection failed; refusing startup without fallback")
        raise
    yield
    dispose_engine()


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type"],
)
app.add_middleware(
    MRIUploadSizeLimitMiddleware, max_file_bytes=settings.mri_max_upload_bytes
)
app.include_router(auth.router, prefix="/api/v1")
app.include_router(chat.router, prefix="/api/v1")
app.include_router(cases.router, prefix="/api/v1")


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": settings.app_name,
        "playground": "/playground",
        "docs": "/docs",
        "health": "/health",
    }


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
        logger.exception("Readiness check failed while connecting to the database")
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ready"})
