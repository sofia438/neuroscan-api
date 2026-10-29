from collections.abc import Generator

from contextlib import nullcontext

from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    url = get_settings().database_url.strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgres://")
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = _database_url()
        settings = get_settings()
        if not url.startswith("postgresql+psycopg://") and settings.environment.lower() != "test":
            raise RuntimeError("The application requires PostgreSQL; SQLite is allowed only with ENVIRONMENT=test")
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=3,
            max_overflow=2,
            connect_args={"prepare_threshold": None} if url.startswith("postgresql+psycopg://") else {},
        )
    return _engine


def database_configured() -> bool:
    return bool(get_settings().database_url.strip())


def get_db() -> Generator[Session, None, None]:
    with sessionmaker(
        bind=get_engine(), autoflush=False, expire_on_commit=False
    )() as session:
        yield session


def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
        _engine = None


def initialize_database(engine: Engine | Connection) -> None:
    """Create current tables and apply the project's small additive legacy upgrades."""
    from app import models  # noqa: F401 - register all model metadata

    Base.metadata.create_all(bind=engine)
    additions = {
        "users": {
            "full_name": "VARCHAR(200) NOT NULL DEFAULT ''",
            "role": "VARCHAR(20) NOT NULL DEFAULT 'patient'",
            "city": "VARCHAR(120)",
            "hospital": "VARCHAR(200)",
            "doctor_verified": "BOOLEAN NOT NULL DEFAULT FALSE",
            "patient_tc_hash": "VARCHAR(64)",
        },
        "chat_messages": {
            "image_urls": "JSON NOT NULL DEFAULT '[]'",
            "image_data": "JSON NOT NULL DEFAULT '[]'",
        },
    }
    inspector = inspect(engine)
    transaction = engine.begin() if isinstance(engine, Engine) else nullcontext(engine)
    with transaction as connection:
        for table, columns in additions.items():
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_patient_tc_hash ON users(patient_tc_hash)"
            )
        )
