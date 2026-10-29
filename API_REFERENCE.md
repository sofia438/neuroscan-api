# API reference and frontend operation map

Base URL for local development: `http://127.0.0.1:8000/api/v1`. OpenAPI is at `http://127.0.0.1:8000/docs`.

## Existing frontend operations and route mapping

| Frontend operation (currently Supabase) | API route | Contract/status |
|---|---|---|
| Register, verify, login, logout, forgot/reset password | `POST /auth/register`, `/auth/verify-email`, `/auth/login`, `/auth/logout`, `/auth/forgot-password`, `/auth/verify-reset-code`, `/auth/reset-password` | Implemented with bearer tokens and email verification. Registration accepts `email`, `password`, `full_name`, `role`, optional `city`, `hospital`. Frontend still uses Supabase flows. |
| Restore session and load profile | `GET /auth/me`, `GET /profile` | Implemented; returns role, doctor information, and whether patient ID is set. |
| Update profile/password | `PATCH /profile`, `POST /auth/change-password` | Implemented; changing password revokes active refresh tokens. |
| Doctor case list/status | `GET /cases`, `GET /cases/{case_id}`, `PATCH /cases/{case_id}` | Implemented with ownership checks; list includes the visit-count shape used by the dashboard. |
| Create patient case and securely retain patient ID | `POST /cases` | Implemented; stores HMAC digest instead of raw ID and links if that patient is registered. |
| Patient links cases with Turkish ID | `PUT /profile/patient-id` | Implemented; validates checksum and links matching cases. |
| Create follow-up, read history, edit notes | `POST /cases/{case_id}/visits`, `GET /cases/{case_id}/visits`, `PATCH /visits/{visit_id}` | Implemented with role and ownership checks. |
| MRI upload and secure view/download | `POST /visits/{visit_id}/uploads`, `GET /cases/{case_id}/uploads`, `GET /uploads/{upload_id}` | Implemented for `.nii` and `.nii.gz`; multipart `file` plus `sequence_name`. Download requires bearer token and ownership. |
| Doctor patient-visibility settings | `GET/PUT /cases/{case_id}/visibility` | Implemented; patient report data is filtered by these settings. |
| Read report and explainability data | `GET /visits/{visit_id}/analysis-results` | Implemented; returns 404 until a genuine result exists. Patient fields are visibility-filtered. |
| Store model output | `POST /visits/{visit_id}/analysis-results` | Implemented for trusted doctor/integration callers. This endpoint accepts supplied results; it does not run a model. |
| Patient chatbot (currently local FAQ) | `POST /chat` and conversation routes | Existing API service is implemented, but the frontend is not connected. Needs `OPENROUTER_API_KEY`. |
| 3D viewer MRI files | uploads routes above | API endpoints exist. NiiVue currently expects Supabase signed URLs and requires frontend adaptation for authenticated downloads. |
| Google sign-in | `GET /auth/google`, `GET /auth/google/callback`, `POST /auth/google/exchange` | Authorization code + PKCE, state and nonce validation, verified Google ID token, patient-only new accounts, no email-based account linking, one-time fragment-code handoff. Requires Google client settings and migration `20261029_0002`. |

## Authentication

Except registration, verification, login, refresh, password recovery, `/health`, `/ready`, and `/playground`, protected routes require `Authorization: Bearer <access_token>`. Login and verification return access and refresh tokens. Refresh with `POST /auth/refresh`; replace both tokens.

Registration JSON:

```json
{"email":"person@example.com","password":"at-least-10-characters","full_name":"Example Person","role":"patient","city":null,"hospital":null}
```

Registration returns 202 and sends a six-digit verification code through Brevo. Valid roles are `doctor` and `patient`. Doctor approval is optional, matching the current frontend feature flag.

Google sign-in starts with a browser navigation to `GET /auth/google`. Google returns to the configured backend callback; the backend redirects to the exact `FRONTEND_URL` with a short-lived `google_oauth_code` in the URL fragment. The frontend immediately removes the fragment and posts the code to `/auth/google/exchange`; tokens are returned in the JSON body, never in a URL. The one-time exchange rejects replays. Existing email accounts are not linked automatically; users must sign in using their existing method.

## Case and upload requests

Doctor case request:

```json
{"id":"C123456","patient_name":"Example Patient","age":42,"sex":"Female","scan_date":"2026-10-28","condition":"tumor","patient_tc":"10000000146"}
```

The Turkish ID is checksum-validated and never stored in plaintext. MRI upload is multipart form data with `sequence_name=T1` and a `file` ending in `.nii` or `.nii.gz`. `MRI_MAX_UPLOAD_BYTES` controls the size limit.

Submit analysis only after a real analysis system returns values:

```json
{"tumor_type":"...","grade":"...","idh_status":"...","mgmt_status":"...","survival_estimate":"...","probabilities":{"class_a":0.0},"xai_features":[{"name":"...","importance":0.0}]}
```

## Health and infrastructure

`GET /health` is a liveness check. `GET /ready` checks database connectivity and returns 503 when unavailable. CORS uses `CORS_ORIGINS`; default origins are local Vite addresses. Set production web origins explicitly and avoid `*`.

## Frontend integration status

The frontend API client uses `VITE_API_BASE_URL=http://127.0.0.1:8000/api/v1` locally. OAuth is not verified until a non-production database has migration `20261029_0002`, the Google Web client callback matches `GOOGLE_REDIRECT_URI`, and a real local sign-in completes successfully.
