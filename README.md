# NeuroScan API

FastAPI backend for NeuroScan account authentication, role-scoped case management, MRI file intake, visit/report storage, and the existing optional chat service. It uses PostgreSQL (for example Neon) and stores uploaded scans on the configured filesystem path. It does not run an MRI model or create predicted results.

## Local setup (PowerShell)

The environment was checked before choosing a version: the Windows `py` launcher does not register an interpreter, but the installed Python 3.14.3 executable at `C:\Users\ASUS\AppData\Local\Programs\Python\Python314\python.exe` works. The pinned dependencies installed under it. From this directory, create/activate the environment:

```powershell
$Python = 'C:\Users\ASUS\AppData\Local\Programs\Python\Python314\python.exe'
& $Python --version
New-Item -ItemType Directory -Force .tmp | Out-Null
$env:TEMP = (Resolve-Path .tmp).Path
$env:TMP = $env:TEMP
if (-not (Test-Path .venv\Scripts\python.exe)) { & $Python -m venv .venv }
.\.venv\Scripts\Activate.ps1
python -m ensurepip --upgrade
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Edit `.env` with Neon's **pooled** PostgreSQL connection URL as `DATABASE_URL` and a random `JWT_SECRET` of at least 32 characters. Keep the password only in `.env` or your deployment secret store. Generate a secret with:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set Brevo sender/key before using email registration, verification, or password recovery. Set `OPENROUTER_API_KEY` to enable `/chat`; case routes do not need it. Set `CORS_ORIGINS` to the exact frontend origin, normally `http://localhost:5173` for Vite. Comma-separated origins are accepted.

For local Google sign-in, configure `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in the backend-only `.env`, with `GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/api/v1/auth/google/callback` and `FRONTEND_URL=http://localhost:5173`. In Google Auth Platform, create a Web application client with that exact redirect URI. Never put the client secret in Vite variables. The backend uses authorization code flow with state, OIDC nonce, PKCE, Google ID-token signature/issuer/audience/email verification, and a short-lived one-time handoff code in the URL fragment; the frontend exchanges that code for NeuroScan tokens. Google identities create patient accounts only. A matching existing email/password account is rejected for manual sign-in; it is never automatically linked.

The `psycopg` 3 driver is installed by `requirements.txt`. PostgreSQL is required for normal startup: a missing URL or failed PostgreSQL connection stops the application, and the backend never falls back to SQLite. SQLite remains available only to isolated unit tests that explicitly set `ENVIRONMENT=test`.

## Database migrations

The previous `create_all` plus additive startup changes did not track schema versions and could not safely manage future changes. Alembic is now the versioned migration mechanism. The baseline migration is additive: it creates missing tables and applies the existing legacy column additions without dropping tables or rows. App startup checks PostgreSQL connectivity only; it does not change the schema. Review and run migrations before starting a new deployment:

```powershell
alembic upgrade head
```

For Neon, use the pooled URL in `DATABASE_URL` for the app. For migrations, prefer Neon's direct (non-pooled) URL in `MIGRATION_DATABASE_URL`, since schema DDL and migration locks are better suited to a direct connection. If no separate migration URL is set, Alembic uses `DATABASE_URL`. Keep both values secret and do not commit `.env`.

Run locally:

```powershell
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation.

Run endpoint and validation tests:

```powershell
pytest -q
```

The PostgreSQL integration test is opt-in and requires `NEUROSCAN_TEST_DATABASE_URL` pointing to a dedicated disposable PostgreSQL database named with a `neuroscan_test` prefix or `_test` suffix. It applies migrations and adds uniquely named fixture rows without truncating or dropping anything. Do not point it at production. Unit tests use isolated SQLite and mocks; they do not verify Neon behavior. MRI files are stored under `MRI_STORAGE_DIR`; use durable private storage in deployment and set `MRI_MAX_UPLOAD_BYTES` for the intended limit.

Example PowerShell commands (replace the placeholder locally with the dedicated test database URL; do not commit it):

```powershell
$env:NEUROSCAN_TEST_DATABASE_URL = 'postgresql://USER:PASSWORD@HOST/neuroscan_test_db?sslmode=require'
pytest -q tests/test_postgres_integration.py
Remove-Item Env:NEUROSCAN_TEST_DATABASE_URL
```

## Frontend connection status

`NeuroScan-main` uses the FastAPI client for authentication, profiles, cases, visits, visibility, uploads, and Google sign-in. The Google flow is only ready after the OAuth migration is applied to a non-production database, client settings are present, and frontend/backend testing succeeds. See [API_REFERENCE.md](API_REFERENCE.md) for the route map.

## Configuration notes

- `.env` is local-only and ignored by Git. `.env.example` contains placeholders.
- Never put PostgreSQL, Brevo, or OpenRouter secrets in frontend variables.
- Local MRI files are not cloud storage. Multi-instance deployments need durable private shared storage or object storage.
- `doctor_verified` is stored for an approval workflow. The current frontend feature flag does not require approval.
- Protected endpoints use bearer tokens and check roles and resource ownership server-side.

The existing OpenRouter chat endpoint remains separate. It needs a configured key and clinical review before use with patients.
