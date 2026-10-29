import asyncio
import gzip
import importlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import models  # register SQLAlchemy metadata
from app.api import auth as auth_api
from app.api import cases as cases_api
from app.api import deps as deps_api
from app.api.auth import router as auth_router
from app.api.cases import router
from app.api.deps import current_user
from app.core.config import Settings, get_settings
from app.core.middleware import MRIUploadSizeLimitMiddleware
from app.db import Base, _database_url, dispose_engine, get_db, get_engine, initialize_database
from app.models import OAuthIdentity, OAuthLoginHandoff, RefreshToken, User
from app.security import hash_token


class LocalClient:
    """In-process ASGI client; avoids binding a local TCP port in restricted Windows runners."""

    def __init__(self, app):
        self.app = app

    def request(self, method, url, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        doctor = User(
            email="doctor@example.test",
            password_hash="x",
            is_verified=True,
            full_name="Doctor One",
            role="doctor",
        )
        patient = User(
            email="patient@example.test",
            password_hash="x",
            is_verified=True,
            full_name="Patient One",
            role="patient",
        )
        session.add_all([doctor, patient])
        session.commit()
        session.refresh(doctor)
        session.refresh(patient)

        app = FastAPI()
        app.add_middleware(MRIUploadSizeLimitMiddleware, max_file_bytes=1024)
        app.include_router(router, prefix="/api/v1")
        app.include_router(auth_router, prefix="/api/v1")
        app.dependency_overrides[get_db] = lambda: session
        app.dependency_overrides[current_user] = lambda: doctor
        test_settings = Settings(
            jwt_secret="test-secret-for-neuroscan-only-1234567890",
            mri_storage_dir=str(tmp_path),
            mri_max_upload_bytes=1024,
        )
        monkeypatch.setattr(cases_api, "get_settings", lambda: test_settings)
        yield LocalClient(app), doctor, patient, session, app
    engine.dispose()


def configure_google_oauth(monkeypatch):
    settings = Settings(
        jwt_secret="google-oauth-tests-only-secret-1234567890",
        google_client_id="test-google-client.apps.googleusercontent.com",
        google_client_secret="test-google-secret",
        google_redirect_uri="http://127.0.0.1:8000/api/v1/auth/google/callback",
        frontend_url="http://localhost:5173",
    )
    monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
    monkeypatch.setattr(deps_api, "get_settings", lambda: settings)
    from app import security

    monkeypatch.setattr(security, "get_settings", lambda: settings)
    claims = {}

    async def fake_exchange(code, verifier, used_settings):
        assert code == "mock-google-code"
        assert verifier
        assert used_settings.google_client_id == settings.google_client_id
        return {"id_token": "mock-google-id-token"}

    monkeypatch.setattr(auth_api, "exchange_google_code", fake_exchange)
    monkeypatch.setattr(
        auth_api,
        "verify_google_id_token",
        lambda token, audience: claims,
    )
    return settings, claims


def begin_google_login(http):
    response = http.get("/api/v1/auth/google")
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    return params["state"][0], params["nonce"][0]


def complete_google_login(http, state, claims, email, subject="google-subject-1"):
    claims.update(
        {
            "iss": "https://accounts.google.com",
            "sub": subject,
            "aud": "test-google-client.apps.googleusercontent.com",
            "email": email,
            "email_verified": True,
            "nonce": claims["expected_nonce"],
            "name": "OAuth Test Patient",
        }
    )
    return http.get(
        "/api/v1/auth/google/callback",
        params={"code": "mock-google-code", "state": state},
    )


def test_route_registration_and_auth_required():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    paths = {route.path for route in app.routes}
    assert "/api/v1/cases" in paths
    assert "/api/v1/visits/{visit_id}/uploads" in paths
    assert "/api/v1/cases/{case_id}/visibility" in paths
    assert "/api/v1/auth/google" in paths
    assert "/api/v1/auth/google/callback" in paths
    assert "/api/v1/auth/google/exchange" in paths
    app.dependency_overrides[get_db] = lambda: None
    http = LocalClient(app)
    assert http.get("/api/v1/cases").status_code == 401


def test_postgresql_database_url_is_normalized_and_never_falls_back(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://tester:secret@db.example.test/neuroscan")
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    dispose_engine()
    try:
        assert _database_url() == "postgresql+psycopg://tester:secret@db.example.test/neuroscan"
        assert get_engine().url.drivername == "postgresql+psycopg"
        assert get_engine().dialect.name == "postgresql"
    finally:
        dispose_engine()
        get_settings.cache_clear()


def test_database_url_is_required_and_sqlite_is_test_only(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ENVIRONMENT", "development")
    get_settings.cache_clear()
    dispose_engine()
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        _database_url()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./should-not-be-used.sqlite")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        get_engine()
    dispose_engine()
    get_settings.cache_clear()


def test_case_validation_and_create(client):
    http, doctor, _, _session, _app = client
    invalid = http.post(
        "/api/v1/cases",
        json={
            "id": "case",
            "patient_name": "",
            "age": 300,
            "sex": "X",
            "scan_date": "2026-01-01",
            "patient_tc": "123",
        },
    )
    assert invalid.status_code == 422
    bad_checksum = http.post(
        "/api/v1/cases",
        json={
            "id": "case",
            "patient_name": "Example",
            "age": 30,
            "sex": "X",
            "scan_date": "2026-01-01",
            "patient_tc": "12345678901",
        },
    )
    assert bad_checksum.status_code == 422
    response = http.post(
        "/api/v1/cases",
        json={
            "id": "C100",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "condition": "tumor",
            "patient_tc": "10000000146",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "Pending"
    assert "patient_tc_hash" not in response.json()
    assert response.json()["doctor_id"] == doctor.id


def test_case_access_is_scoped_to_role(client):
    http, doctor, patient, session, app = client
    http.post(
        "/api/v1/cases",
        json={
            "id": "C200",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    app.dependency_overrides[current_user] = lambda: patient
    assert http.get("/api/v1/cases/C200").status_code == 404
    assert (
        http.post(
            "/api/v1/cases",
            json={
                "id": "C201",
                "patient_name": "Example",
                "age": 20,
                "sex": "X",
                "scan_date": "2026-01-01",
                "patient_tc": "10000000146",
            },
        ).status_code
        == 403
    )
    app.dependency_overrides[current_user] = lambda: doctor
    response = http.post(
        "/api/v1/cases/C200/visits",
        json={"visit_date": "2026-02-01", "notes": "follow-up"},
    )
    assert response.status_code == 201
    assert response.json()["status"] == "Pending"
    assert http.patch("/api/v1/cases/C200", json={"status": []}).status_code == 422
    other = User(
        email="other-doctor@example.com",
        password_hash="x",
        is_verified=True,
        full_name="Other Doctor",
        role="doctor",
    )
    session.add(other)
    session.commit()
    session.refresh(other)
    app.dependency_overrides[current_user] = lambda: other
    assert http.get("/api/v1/cases/C200").status_code == 404


def test_analysis_is_missing_until_real_result_is_submitted(client):
    http, _, _, _, _ = client
    http.post(
        "/api/v1/cases",
        json={
            "id": "C300",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    visit = http.post(
        "/api/v1/cases/C300/visits", json={"visit_date": "2026-02-01"}
    ).json()
    assert http.get(f"/api/v1/visits/{visit['id']}/analysis-results").status_code == 404


def test_visibility_contract_and_file_type_validation(client):
    http, _, _, _, _ = client
    http.post(
        "/api/v1/cases",
        json={
            "id": "C400",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    visibility = http.put(
        "/api/v1/cases/C400/visibility",
        json={"tumor_type": True, "doctor_note": "Discuss in clinic"},
    )
    assert visibility.status_code == 200
    assert visibility.json()["tumor_type"] is True
    visit = http.post(
        "/api/v1/cases/C400/visits", json={"visit_date": "2026-02-01"}
    ).json()
    upload = http.post(
        f"/api/v1/visits/{visit['id']}/uploads",
        data={"sequence_name": "T1"},
        files={"file": ("scan.txt", b"not an MRI", "text/plain")},
    )
    assert upload.status_code == 415


def test_upload_persists_metadata_and_download_is_owner_scoped(client):
    http, _doctor, patient, session, app = client
    created = http.post(
        "/api/v1/cases",
        json={
            "id": "C500",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    assert created.status_code == 201
    visit = http.post(
        "/api/v1/cases/C500/visits", json={"visit_date": "2026-02-01"}
    ).json()
    content = gzip.compress(bytes([0x5c, 0x01, 0, 0]) + bytes(348))
    upload = http.post(
        f"/api/v1/visits/{visit['id']}/uploads",
        data={"sequence_name": "T1"},
        files={"file": ("T1.nii.gz", content, "application/gzip")},
    )
    assert upload.status_code == 201
    metadata = upload.json()
    assert metadata["file_name"] == "T1.nii.gz"
    assert (
        http.get("/api/v1/cases/C500/uploads", params={"visit_id": visit["id"]}).json()[
            0
        ]["id"]
        == metadata["id"]
    )
    download = http.get(metadata["download_url"])
    assert download.status_code == 200
    assert download.content == content
    assert Path(session.get(models.MRIUpload, metadata["id"]).file_path).is_file()
    app.dependency_overrides[current_user] = lambda: patient
    assert http.get(metadata["download_url"]).status_code == 404


def test_upload_limit_removes_partial_file_and_rejects_unsupported_sequence(client):
    http, _, _, session, _ = client
    http.post(
        "/api/v1/cases",
        json={
            "id": "C600",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    visit = http.post(
        "/api/v1/cases/C600/visits", json={"visit_date": "2026-02-01"}
    ).json()
    too_big = http.post(
        f"/api/v1/visits/{visit['id']}/uploads",
        data={"sequence_name": "T1"},
        files={"file": ("large.nii", b"x" * 1025, "application/octet-stream")},
    )
    assert too_big.status_code == 413
    bad_sequence = http.post(
        f"/api/v1/visits/{visit['id']}/uploads",
        data={"sequence_name": "../T1"},
        files={"file": ("T1.nii", b"x", "application/octet-stream")},
    )
    assert bad_sequence.status_code == 422
    assert http.get("/api/v1/cases/C600/uploads").json() == []
    assert session.query(models.MRIUpload).count() == 0


def test_upload_rejects_invalid_nifti_header(client):
    http, _, _, session, _ = client
    http.post("/api/v1/cases", json={"id":"C601", "patient_name":"Example Patient", "age":42, "sex":"Female", "scan_date":"2026-01-01", "patient_tc":"10000000146"})
    visit = http.post("/api/v1/cases/C601/visits", json={"visit_date":"2026-02-01"}).json()
    response = http.post(f"/api/v1/visits/{visit['id']}/uploads", data={"sequence_name":"T1"}, files={"file":("fake.nii", b"not a NIfTI image", "application/octet-stream")})
    assert response.status_code == 415
    assert session.query(models.MRIUpload).count() == 0


def test_multipart_request_limit_rejects_before_form_parsing(client):
    http, _, _, session, _ = client
    # Per-file limit is 1 KiB; middleware allows 256 KiB of multipart framing.
    response = http.post("/api/v1/visits/1/uploads", content=b"x" * (1024 + 256 * 1024 + 1), headers={"Content-Type":"multipart/form-data; boundary=x"})
    assert response.status_code == 413


def test_patient_linking_visibility_and_analysis_filtering(client):
    http, doctor, patient, session, app = client
    tc = "10000000146"
    patient.patient_tc_hash = cases_api.tc_digest(tc)
    session.commit()
    assert (
        http.post(
            "/api/v1/cases",
            json={
                "id": "C700",
                "patient_name": "Example Patient",
                "age": 42,
                "sex": "Female",
                "scan_date": "2026-01-01",
                "patient_tc": tc,
            },
        ).status_code
        == 201
    )
    visit = http.post(
        "/api/v1/cases/C700/visits", json={"visit_date": "2026-02-01"}
    ).json()
    result = {
        "tumor_type": "test-class",
        "grade": "test-grade",
        "idh_status": "test-marker",
        "survival_estimate": "test-only",
        "probabilities": {"test-class": 88.0},
        "xai_features": [{"name": "test-feature", "importance": 12.0}],
    }
    saved = http.post(f"/api/v1/visits/{visit['id']}/analysis-results", json=result)
    assert saved.status_code == 201
    assert saved.json()["tumor_type"] == "test-class"
    assert http.get("/api/v1/cases/C700").json()["status"] == "Done"
    next_visit = http.post(
        "/api/v1/cases/C700/visits", json={"visit_date": "2026-03-01"}
    )
    assert next_visit.status_code == 201
    assert http.get("/api/v1/cases/C700").json()["status"] == "Pending"
    assert (
        http.patch(
            "/api/v1/visits/" + str(visit["id"]), json={"notes": "updated"}
        ).status_code
        == 200
    )
    app.dependency_overrides[current_user] = lambda: patient
    assert http.get("/api/v1/cases/C700").status_code == 200
    assert http.get(f"/api/v1/visits/{visit['id']}/analysis-results").json() == {}
    patient_visits = http.get("/api/v1/cases/C700/visits").json()
    assert "notes" not in patient_visits[0]
    app.dependency_overrides[current_user] = lambda: doctor
    visibility = http.put(
        "/api/v1/cases/C700/visibility",
        json={"tumor_type": True, "clinical_markers": True, "xai_explanation": True},
    )
    assert visibility.status_code == 200
    app.dependency_overrides[current_user] = lambda: patient
    visible = http.get(f"/api/v1/visits/{visit['id']}/analysis-results").json()
    assert visible == {
        "tumor_type": "test-class",
        "grade": "test-grade",
        "idh_status": "test-marker",
        "mgmt_status": None,
        "xai_features": [{"name": "test-feature", "importance": 12.0}],
    }
    assert (
        http.post(
            "/api/v1/cases/C700/visits", json={"visit_date": "2026-03-01"}
        ).status_code
        == 403
    )


def test_patient_id_claim_links_orphan_cases_without_returning_raw_id(client):
    http, _, patient, _session, app = client
    http.post(
        "/api/v1/cases",
        json={
            "id": "C710",
            "patient_name": "Example Patient",
            "age": 42,
            "sex": "Female",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    app.dependency_overrides[current_user] = lambda: patient
    response = http.put(
        "/api/v1/profile/patient-id", json={"patient_tc": "10000000146"}
    )
    assert response.status_code == 200
    assert response.json() == {"has_tc": True, "linked": 1}
    assert patient.patient_tc_hash == cases_api.tc_digest("10000000146")
    assert "10000000146" not in patient.patient_tc_hash
    assert http.get("/api/v1/cases/C710").status_code == 200
    assert (
        http.put(
            "/api/v1/profile/patient-id", json={"patient_tc": "12345678901"}
        ).status_code
        == 422
    )


def test_register_login_verify_refresh_and_logout_without_email_service(
    client, monkeypatch
):
    http, _, _, _, app = client
    app.dependency_overrides.pop(current_user, None)
    settings = Settings(jwt_secret="auth-tests-only-secret-key-1234567890")
    monkeypatch.setattr(auth_api, "get_settings", lambda: settings)
    monkeypatch.setattr(deps_api, "get_settings", lambda: settings)
    from app import security

    monkeypatch.setattr(security, "get_settings", lambda: settings)
    sent = {}

    async def fake_send(email, code):
        sent.update(email=email, code=code)

    monkeypatch.setattr(auth_api, "send_verification_code", fake_send)
    registration = http.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "long-enough-test-pass",
            "full_name": "Test Person",
            "role": "patient",
        },
    )
    assert registration.status_code == 202
    assert sent["email"] == "new@example.com"
    assert (
        http.post(
            "/api/v1/auth/login",
            json={"email": "new@example.com", "password": "long-enough-test-pass"},
        ).status_code
        == 403
    )
    verified = http.post(
        "/api/v1/auth/verify-email", json={"email": sent["email"], "code": sent["code"]}
    )
    assert verified.status_code == 200
    verify_tokens = verified.json()
    headers = {"Authorization": f"Bearer {verify_tokens['access_token']}"}
    assert http.get("/api/v1/auth/me", headers=headers).json()["role"] == "patient"
    assert (
        http.post(
            "/api/v1/auth/login",
            json={"email": "new@example.com", "password": "wrong-password"},
        ).status_code
        == 401
    )
    login = http.post(
        "/api/v1/auth/login",
        json={"email": "new@example.com", "password": "long-enough-test-pass"},
    )
    assert login.status_code == 200
    tokens = login.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    rotated = http.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert rotated.status_code == 200
    rotated_tokens = rotated.json()
    rotated_headers = {"Authorization": f"Bearer {rotated_tokens['access_token']}"}
    assert (
        http.post(
            "/api/v1/auth/logout",
            json={"refresh_token": rotated_tokens["refresh_token"]},
            headers=rotated_headers,
        ).status_code
        == 204
    )
    assert (
        http.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": rotated_tokens["refresh_token"]},
        ).status_code
        == 401
    )
    assert (
        http.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "long-enough-test-pass",
                "new_password": "another-test-password",
            },
            headers=headers,
        ).status_code
        == 204
    )
    assert (
        http.post(
            "/api/v1/auth/login",
            json={"email": "new@example.com", "password": "long-enough-test-pass"},
        ).status_code
        == 401
    )
    assert (
        http.post(
            "/api/v1/auth/login",
            json={"email": "new@example.com", "password": "another-test-password"},
        ).status_code
        == 200
    )


def test_google_oauth_rejects_invalid_state_without_exchanging_code(client, monkeypatch):
    http, _, _, session, _ = client
    configure_google_oauth(monkeypatch)
    response = http.get(
        "/api/v1/auth/google/callback",
        params={"code": "mock-google-code", "state": "not-a-real-state"},
    )
    assert response.status_code == 400
    assert session.query(User).count() == 2
    assert session.query(OAuthLoginHandoff).count() == 0


def test_google_oauth_rejects_invalid_google_id_token(client, monkeypatch):
    http, _, _, session, _ = client
    _, claims = configure_google_oauth(monkeypatch)
    state, nonce = begin_google_login(http)
    claims["expected_nonce"] = nonce

    def reject_token(*_args):
        raise ValueError("invalid signature, audience, issuer, or claims")

    monkeypatch.setattr(auth_api, "verify_google_id_token", reject_token)
    response = http.get(
        "/api/v1/auth/google/callback",
        params={"code": "mock-google-code", "state": state},
    )
    assert response.status_code == 303
    assert "google_oauth_error=authentication_failed" in response.headers["location"]
    assert session.query(User).count() == 2
    assert session.query(OAuthIdentity).count() == 0


def test_google_oauth_creates_patient_and_exchanges_single_use_handoff(
    client, monkeypatch
):
    http, _, _, session, app = client
    settings, claims = configure_google_oauth(monkeypatch)
    state, nonce = begin_google_login(http)
    claims["expected_nonce"] = nonce
    callback = complete_google_login(http, state, claims, "new-google@example.test")
    assert callback.status_code == 303
    fragment = parse_qs(urlsplit(callback.headers["location"]).fragment)
    assert "access_token" not in fragment and "refresh_token" not in fragment
    handoff_code = fragment["google_oauth_code"][0]
    user = session.query(User).filter_by(email="new-google@example.test").one()
    assert user.role == "patient"
    assert user.is_verified is True
    assert user.doctor_verified is False
    assert user.password_hash != "mock-google-id-token"
    assert session.query(OAuthIdentity).filter_by(user_id=user.id).one().subject == "google-subject-1"

    # Exercise the real bearer-token dependency after the one-time exchange.
    app.dependency_overrides.pop(current_user, None)
    tokens_response = http.post(
        "/api/v1/auth/google/exchange", json={"code": handoff_code}
    )
    assert tokens_response.status_code == 200
    tokens = tokens_response.json()
    access_claims = jwt.decode(
        tokens["access_token"], options={"verify_signature": False}
    )
    assert access_claims["sub"] == str(user.id)
    assert session.query(RefreshToken).filter_by(user_id=user.id).count() == 1
    profile = http.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert profile.status_code == 200
    assert profile.json()["id"] == user.id
    assert profile.json()["role"] == "patient"
    assert http.post(
        "/api/v1/auth/google/exchange", json={"code": handoff_code}
    ).status_code == 400
    assert settings.google_client_id == "test-google-client.apps.googleusercontent.com"


def test_google_oauth_reuses_existing_google_identity(client, monkeypatch):
    http, _, patient, session, _ = client
    _, claims = configure_google_oauth(monkeypatch)
    session.add(
        OAuthIdentity(
            user_id=patient.id,
            provider="google",
            subject="known-google-subject",
            email=patient.email,
        )
    )
    session.commit()
    state, nonce = begin_google_login(http)
    claims["expected_nonce"] = nonce
    response = complete_google_login(
        http, state, claims, patient.email, subject="known-google-subject"
    )
    assert response.status_code == 303
    assert "google_oauth_code=" in response.headers["location"]
    assert session.query(User).count() == 2
    assert session.query(OAuthIdentity).filter_by(user_id=patient.id).count() == 1


def test_google_oauth_does_not_link_existing_password_account(client, monkeypatch):
    http, _, patient, session, _ = client
    _, claims = configure_google_oauth(monkeypatch)
    state, nonce = begin_google_login(http)
    claims["expected_nonce"] = nonce
    response = complete_google_login(
        http, state, claims, patient.email, subject="unlinked-google-subject"
    )
    assert response.status_code == 303
    assert "google_oauth_error=account_conflict" in response.headers["location"]
    assert session.query(User).count() == 2
    assert session.query(OAuthIdentity).count() == 0


def test_doctor_approval_configuration_blocks_unverified_doctors(client, monkeypatch):
    http, _, _, _, _ = client
    settings = Settings(
        jwt_secret="approval-test-secret-123456789012", doctor_approval_required=True
    )
    monkeypatch.setattr(cases_api, "get_settings", lambda: settings)
    response = http.post(
        "/api/v1/cases",
        json={
            "id": "C800",
            "patient_name": "Example",
            "age": 20,
            "sex": "X",
            "scan_date": "2026-01-01",
            "patient_tc": "10000000146",
        },
    )
    assert response.status_code == 403


def test_additive_database_migration_is_idempotent_on_sqlite(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    legacy = MetaData()
    Table(
        "users",
        legacy,
        Column("id", Integer, primary_key=True),
        Column("email", String(320)),
        Column("password_hash", String(255)),
        Column("is_verified", Boolean),
        Column("created_at", DateTime),
    )
    Table(
        "chat_messages",
        legacy,
        Column("id", Integer, primary_key=True),
        Column("conversation_id", Integer),
        Column("role", String(20)),
        Column("content", String),
        Column("created_at", DateTime),
    )
    legacy.create_all(engine)
    initialize_database(engine)
    initialize_database(engine)
    user_cols = {column["name"] for column in inspect(engine).get_columns("users")}
    chat_cols = {
        column["name"] for column in inspect(engine).get_columns("chat_messages")
    }
    assert {"full_name", "role", "doctor_verified", "patient_tc_hash"} <= user_cols
    assert {"image_urls", "image_data"} <= chat_cols
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
    engine.dispose()


def test_alembic_baseline_preserves_existing_sqlite_data(monkeypatch, tmp_path):
    db_path = tmp_path / "legacy-alembic.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(320), password_hash VARCHAR(255), is_verified BOOLEAN, created_at DATETIME)"))
        connection.execute(text("INSERT INTO users (id, email, password_hash, is_verified) VALUES (17, 'existing@example.test', 'kept', 1)"))
    engine.dispose()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT email, password_hash FROM users WHERE id=17")).one() == ("existing@example.test", "kept")
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "20261029_0002"
            assert "cases" in inspect(connection).get_table_names()
        engine.dispose()
    finally:
        get_settings.cache_clear()


def test_app_startup_uses_explicit_test_sqlite_and_registers_routes(monkeypatch, tmp_path):
    db_path = tmp_path / "startup.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("JWT_SECRET", "startup-test-secret-12345678901234567890")
    get_settings.cache_clear()
    import main

    main = importlib.reload(main)
    initialize_database(main.get_engine())

    async def run_lifespan():
        async with main.lifespan(main.app):
            assert main.database_configured()
            with main.get_engine().connect() as connection:
                assert connection.execute(text("SELECT 1")).scalar_one() == 1
            paths = {route.path for route in main.app.routes if hasattr(route, "path")}
            assert "/api/v1/auth/login" in paths
            assert "/api/v1/cases" in paths
            assert "/api/v1/visits/{visit_id}/uploads" in paths

    try:
        asyncio.run(run_lifespan())
    finally:
        get_settings.cache_clear()


def test_cors_allows_configured_origin_and_rejects_unlisted_origin():
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
    )
    http = LocalClient(app)
    allowed = http.request(
        "OPTIONS",
        "/anything",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    rejected = http.request(
        "OPTIONS",
        "/anything",
        headers={
            "Origin": "https://unlisted.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "access-control-allow-origin" not in rejected.headers
