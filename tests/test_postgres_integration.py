"""Opt-in integration checks for a dedicated PostgreSQL test database.

Set NEUROSCAN_TEST_DATABASE_URL to a disposable database whose name contains
"test". The tests never truncate or drop tables and use unique row identifiers.
"""
import os
import uuid
from datetime import date

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.engine import make_url

from app.db import Base
from app.core.config import get_settings
from app.models import AnalysisResult, Case, MRIUpload, User, Visit


def _test_database_url() -> str:
    raw = os.environ.get("NEUROSCAN_TEST_DATABASE_URL", "").strip()
    if not raw:
        pytest.skip("Set NEUROSCAN_TEST_DATABASE_URL to run PostgreSQL integration tests")
    url = make_url(raw)
    if url.get_backend_name() != "postgresql":
        pytest.fail("NEUROSCAN_TEST_DATABASE_URL must point to PostgreSQL")
    database = (url.database or "").lower()
    if not (database.startswith("neuroscan_test") or database.endswith("_test")):
        pytest.fail("Refusing integration tests unless the database name starts with 'neuroscan_test' or ends with '_test'")
    if url.drivername in {"postgresql", "postgres"}:
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def test_postgresql_schema_and_domain_transactions(monkeypatch):
    database_url = _test_database_url()
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "")
    get_settings.cache_clear()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"prepare_threshold": None},
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one()

        config = Config("alembic.ini")
        command.upgrade(config, "head")
        assert "users" in Base.metadata.tables
        with engine.connect() as connection:
            table_names = set(__import__("sqlalchemy").inspect(connection).get_table_names())
        assert {"users", "cases", "visits", "mri_uploads", "analysis_results"} <= table_names

        marker = uuid.uuid4().hex
        user_id = case_id = visit_id = upload_id = result_id = None
        with Session(engine, expire_on_commit=False) as session:
            user = User(
                email=f"pg-{marker}@example.test",
                password_hash="test-hash",
                is_verified=True,
                full_name="Integration Doctor",
                role="doctor",
            )
            session.add(user)
            session.flush()
            case = Case(
                id=f"PG-{marker}",
                patient_name="Integration Patient",
                age=40,
                sex="Unspecified",
                scan_date=date(2026, 10, 29),
                doctor_id=user.id,
            )
            session.add(case)
            session.flush()
            visit = Visit(case_id=case.id, visit_date=date(2026, 10, 29))
            session.add(visit)
            session.flush()
            upload = MRIUpload(
                case_id=case.id,
                visit_id=visit.id,
                sequence_name="T1",
                file_name="metadata-only.nii",
                file_path=f"/integration-tests/{marker}.nii",
            )
            result = AnalysisResult(
                case_id=case.id,
                visit_id=visit.id,
                tumor_type="test fixture",
                probabilities={"test": 1.0},
                xai_features=[{"name": "fixture", "weight": 1.0}],
            )
            session.add_all([upload, result])
            session.flush()
            user_id, case_id, visit_id = user.id, case.id, visit.id
            upload_id, result_id = upload.id, result.id
            session.commit()

            assert session.get(User, user_id).email == f"pg-{marker}@example.test"
            assert session.get(Case, case_id).doctor_id == user_id
            assert session.get(Visit, visit_id).case_id == case_id
            assert session.get(MRIUpload, upload_id).visit_id == visit_id
            assert session.get(AnalysisResult, result_id).probabilities == {"test": 1.0}

            duplicate = User(
                email=f"pg-{marker}@example.test",
                password_hash="test-hash",
                is_verified=True,
                role="doctor",
            )
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                session.flush()
            session.rollback()

            rolled_back = User(
                email=f"rollback-{marker}@example.test",
                password_hash="test-hash",
                role="doctor",
            )
            session.add(rolled_back)
            session.flush()
            rolled_back_id = rolled_back.id
            session.rollback()
            assert session.get(User, rolled_back_id) is None
    finally:
        engine.dispose()
        get_settings.cache_clear()
