import hmac
import logging
import secrets
import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from jwt import PyJWKClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import get_settings
from app.db import get_db
from app.models import (
    EmailOTP,
    PasswordResetGrant,
    PasswordResetOTP,
    OAuthIdentity,
    OAuthLoginHandoff,
    OAuthLoginState,
    RefreshToken,
    User,
)
from app.schemas import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResendOTPRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
    VerifyEmailRequest,
    VerifyResetCodeRequest,
    OAuthExchangeRequest,
)
from app.security import (
    create_access_token,
    create_refresh_token,
    hash_otp,
    hash_password,
    hash_token,
    new_otp,
    verify_password,
)
from app.services.email import send_password_reset_code, send_verification_code

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_ENDPOINT = "https://www.googleapis.com/oauth2/v3/certs"
_google_jwks = PyJWKClient(
    GOOGLE_JWKS_ENDPOINT, cache_jwk_set=True, lifespan=300, timeout=5
)


def _google_settings():
    settings = get_settings()
    if not settings.google_client_id.strip() or not settings.google_client_secret.strip():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")
    redirect = urlsplit(settings.google_redirect_uri)
    if (
        redirect.scheme not in {"http", "https"}
        or not redirect.netloc
        or redirect.query
        or redirect.fragment
        or redirect.username
        or (settings.environment.lower() == "production" and redirect.scheme != "https")
    ):
        raise HTTPException(status_code=503, detail="Google callback URL is not configured safely")
    frontend = urlsplit(settings.frontend_url)
    if (
        frontend.scheme not in {"http", "https"}
        or not frontend.netloc
        or frontend.query
        or frontend.fragment
        or frontend.username
        or (settings.environment.lower() == "production" and frontend.scheme != "https")
    ):
        raise HTTPException(status_code=503, detail="Frontend URL is not configured safely")
    return settings


def _frontend_result(settings, **fragment_values) -> RedirectResponse:
    base = settings.frontend_url.rstrip("/")
    return RedirectResponse(
        f"{base}/#{urlencode(fragment_values)}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def exchange_google_code(code: str, code_verifier: str, settings) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        return response.json()


def verify_google_id_token(id_token: str, client_id: str) -> dict:
    signing_key = _google_jwks.get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(
        id_token,
        signing_key,
        algorithms=["RS256"],
        audience=client_id,
        issuer=["https://accounts.google.com", "accounts.google.com"],
        options={"require": ["exp", "iat", "iss", "aud", "sub", "email", "email_verified", "nonce"]},
        leeway=60,
    )
    audiences = claims.get("aud")
    if isinstance(audiences, list) and len(audiences) > 1 and claims.get("azp") != client_id:
        raise ValueError("Unexpected authorized party")
    if claims.get("email_verified") is not True:
        raise ValueError("Google email is not verified")
    return claims


@router.get("/google")
def google_login(db: Session = Depends(get_db)) -> RedirectResponse:
    settings = _google_settings()
    now = datetime.now(UTC)
    db.query(OAuthLoginState).filter(OAuthLoginState.expires_at <= now).delete(
        synchronize_session=False
    )
    db.query(OAuthLoginHandoff).filter(
        OAuthLoginHandoff.expires_at <= now
    ).delete(synchronize_session=False)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    db.add(
        OAuthLoginState(
            state_hash=hash_token(state),
            nonce=nonce,
            code_verifier=verifier,
            expires_at=now + timedelta(seconds=settings.google_state_ttl_seconds),
        )
    )
    db.commit()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge.rstrip(b"=").decode("ascii"),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return RedirectResponse(
        f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}", status_code=302
    )


@router.get("/google/callback")
async def google_callback(
    db: Session = Depends(get_db),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=100),
) -> RedirectResponse:
    settings = _google_settings()
    if not state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")
    record = db.scalar(
        select(OAuthLoginState)
        .where(OAuthLoginState.state_hash == hash_token(state))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if not record or record.expires_at.replace(tzinfo=UTC) <= now:
        if record:
            db.delete(record)
            db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    nonce, verifier = record.nonce, record.code_verifier
    db.delete(record)
    db.commit()
    if error or not code:
        return _frontend_result(settings, google_oauth_error="cancelled")

    try:
        token_response = await exchange_google_code(code, verifier, settings)
        claims = await asyncio.to_thread(
            verify_google_id_token, token_response["id_token"], settings.google_client_id
        )
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("Invalid OIDC nonce")
        subject = str(claims["sub"])
        email = str(claims["email"]).strip().lower()
        if not subject or len(subject) > 255 or not email or len(email) > 320:
            raise ValueError("Required identity claims are invalid")
    except Exception:
        logger.warning("Google OAuth callback rejected an invalid or unavailable identity")
        return _frontend_result(settings, google_oauth_error="authentication_failed")

    identity = db.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == "google", OAuthIdentity.subject == subject
        )
    )
    try:
        if identity:
            user = db.get(User, identity.user_id)
            if not user or not user.is_verified or user.role != "patient":
                return _frontend_result(settings, google_oauth_error="account_conflict")
        else:
            existing = db.scalar(select(User).where(func.lower(User.email) == email))
            if existing:
                # Never link an existing password account based only on a matching email.
                return _frontend_result(settings, google_oauth_error="account_conflict")
            display_name = claims.get("name")
            display_name = display_name.strip()[:200] if isinstance(display_name, str) else ""
            user = User(
                email=email,
                password_hash=hash_password(secrets.token_urlsafe(48)),
                is_verified=True,
                full_name=display_name or "NeuroScan Patient",
                role="patient",
                doctor_verified=False,
            )
            db.add(user)
            db.flush()
            db.add(
                OAuthIdentity(
                    user_id=user.id,
                    provider="google",
                    subject=subject,
                    email=email,
                )
            )

        handoff = secrets.token_urlsafe(48)
        db.add(
            OAuthLoginHandoff(
                code_hash=hash_token(handoff),
                user_id=user.id,
                expires_at=now + timedelta(seconds=settings.google_handoff_ttl_seconds),
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("Google OAuth account creation collided with an existing identity")
        return _frontend_result(settings, google_oauth_error="account_conflict")
    return _frontend_result(settings, google_oauth_code=handoff)


@router.post("/google/exchange", response_model=TokenResponse)
def google_exchange(
    payload: OAuthExchangeRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    handoff = db.scalar(
        select(OAuthLoginHandoff)
        .where(OAuthLoginHandoff.code_hash == hash_token(payload.code))
        .with_for_update()
    )
    now = datetime.now(UTC)
    if (
        not handoff
        or handoff.consumed_at is not None
        or handoff.expires_at.replace(tzinfo=UTC) <= now
    ):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth handoff")
    user = db.get(User, handoff.user_id)
    if not user or not user.is_verified or user.role != "patient":
        raise HTTPException(status_code=400, detail="Invalid OAuth handoff")
    handoff.consumed_at = now
    # token_pair commits the handoff consumption and refresh-token creation together.
    return token_pair(user, db)


async def issue_otp(user: User, db: Session) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    record = db.scalar(select(EmailOTP).where(EmailOTP.user_id == user.id))
    if (
        record
        and record.sent_at.replace(tzinfo=UTC)
        + timedelta(seconds=settings.otp_resend_seconds)
        > now
    ):
        raise HTTPException(
            status_code=429, detail="Please wait before requesting another code"
        )
    code = new_otp()
    if record is None:
        record = EmailOTP(
            user_id=user.id,
            code_hash=hash_otp(user.email, code),
            expires_at=now + timedelta(minutes=settings.otp_expiry_minutes),
            sent_at=now,
            attempts=0,
        )
        db.add(record)
    else:
        record.code_hash = hash_otp(user.email, code)
        record.expires_at = now + timedelta(minutes=settings.otp_expiry_minutes)
        record.sent_at = now
        record.attempts = 0
    db.commit()
    try:
        await send_verification_code(user.email, code)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.exception(
            "Verification email delivery failed; registration remains pending (%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Could not send verification email. Try again shortly.",
        ) from exc


def token_pair(user: User, db: Session) -> TokenResponse:
    refresh, expires = create_refresh_token(user.id)
    db.add(
        RefreshToken(
            user_id=user.id, token_hash=hash_token(refresh), expires_at=expires
        )
    )
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id),
        refresh_token=refresh,
        expires_in=get_settings().access_token_minutes * 60,
    )


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
async def register(
    payload: RegisterRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and user.is_verified:
        raise HTTPException(
            status_code=409, detail="An account with this email already exists"
        )
    if user is None:
        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name.strip(),
            role=payload.role,
            city=payload.city,
            hospital=payload.hospital,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    await issue_otp(user, db)
    return {
        "message": "If this address can be registered, a verification code has been sent."
    }


@router.post("/verify-email", response_model=TokenResponse)
def verify_email(
    payload: VerifyEmailRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    record = (
        db.scalar(select(EmailOTP).where(EmailOTP.user_id == user.id)) if user else None
    )
    if not user or not record:
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification code"
        )
    now = datetime.now(UTC)
    if (
        record.expires_at.replace(tzinfo=UTC) <= now
        or record.attempts >= get_settings().otp_max_attempts
    ):
        db.delete(record)
        db.commit()
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification code"
        )
    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_otp(email, payload.code)):
        db.commit()
        raise HTTPException(
            status_code=400, detail="Invalid or expired verification code"
        )
    user.is_verified = True
    db.delete(record)
    db.commit()
    return token_pair(user, db)


@router.post("/resend-code", status_code=status.HTTP_202_ACCEPTED)
async def resend_code(
    payload: ResendOTPRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and not user.is_verified:
        await issue_otp(user, db)
    return {"message": "If the account is awaiting verification, a code has been sent."}


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(User.email == str(payload.email).lower()))
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="Email verification is required")
    return token_pair(user, db)


@router.post("/refresh", response_model=TokenResponse)
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> TokenResponse:
    record = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token)
        )
    )
    now = datetime.now(UTC)
    if not record or record.revoked_at or record.expires_at.replace(tzinfo=UTC) <= now:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = db.get(User, record.user_id)
    if not user or not user.is_verified:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    record.revoked_at = now
    db.commit()
    return token_pair(user, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    record = db.scalar(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(payload.refresh_token),
            RefreshToken.user_id == user.id,
        )
    )
    if record and not record.revoked_at:
        record.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)) -> User:
    return UserResponse.from_user(user)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.password_hash = hash_password(payload.new_password)
    now = datetime.now(UTC)
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id,
        RefreshToken.revoked_at.is_(None),
    ).update({RefreshToken.revoked_at: now}, synchronize_session=False)
    db.commit()


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: ForgotPasswordRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Always return the same response so callers cannot discover registered emails."""
    email = str(payload.email).lower()
    user = db.scalar(
        select(User).where(User.email == email, User.is_verified.is_(True))
    )
    if not user:
        return {
            "message": "If an account exists for this address, a password reset code has been sent."
        }

    settings = get_settings()
    now = datetime.now(UTC)
    record = db.scalar(
        select(PasswordResetOTP)
        .where(PasswordResetOTP.user_id == user.id)
        .with_for_update()
    )
    if (
        record
        and record.sent_at.replace(tzinfo=UTC)
        + timedelta(seconds=settings.otp_resend_seconds)
        > now
    ):
        return {
            "message": "If an account exists for this address, a password reset code has been sent."
        }

    code = new_otp()
    if record is None:
        record = PasswordResetOTP(
            user_id=user.id,
            code_hash=hash_otp(email, code),
            expires_at=now + timedelta(minutes=settings.otp_expiry_minutes),
            sent_at=now,
            attempts=0,
        )
        db.add(record)
    else:
        record.code_hash = hash_otp(email, code)
        record.expires_at = now + timedelta(minutes=settings.otp_expiry_minutes)
        record.sent_at = now
        record.attempts = 0
    # A newly requested code invalidates reset grants issued from earlier codes.
    db.query(PasswordResetGrant).filter(
        PasswordResetGrant.user_id == user.id,
        PasswordResetGrant.consumed_at.is_(None),
        PasswordResetGrant.revoked_at.is_(None),
    ).update({PasswordResetGrant.revoked_at: now}, synchronize_session=False)
    db.commit()

    try:
        await send_password_reset_code(email, code)
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.exception(
            "Password reset email delivery failed (%s)", type(exc).__name__
        )
        db.delete(record)
        db.commit()
    return {
        "message": "If an account exists for this address, a password reset code has been sent."
    }


@router.post("/verify-reset-code")
def verify_reset_code(
    payload: VerifyResetCodeRequest, db: Session = Depends(get_db)
) -> dict[str, int | str]:
    email = str(payload.email).lower()
    user = db.scalar(
        select(User).where(User.email == email, User.is_verified.is_(True))
    )
    record = (
        db.scalar(
            select(PasswordResetOTP)
            .where(PasswordResetOTP.user_id == user.id)
            .with_for_update()
        )
        if user
        else None
    )
    now = datetime.now(UTC)
    if (
        not user
        or not record
        or record.expires_at.replace(tzinfo=UTC) <= now
        or record.attempts >= get_settings().otp_max_attempts
    ):
        if record:
            db.delete(record)
            db.commit()
        raise HTTPException(
            status_code=400, detail="Invalid or expired password reset code"
        )

    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_otp(email, payload.code)):
        db.commit()
        raise HTTPException(
            status_code=400, detail="Invalid or expired password reset code"
        )

    settings = get_settings()
    raw_token = secrets.token_urlsafe(48)
    db.add(
        PasswordResetGrant(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=now + timedelta(minutes=settings.password_reset_token_minutes),
        )
    )
    db.delete(record)
    db.commit()
    return {
        "reset_token": raw_token,
        "expires_in": settings.password_reset_token_minutes * 60,
    }


@router.post("/reset-password")
def reset_password(
    payload: ResetPasswordRequest, db: Session = Depends(get_db)
) -> dict[str, str]:
    now = datetime.now(UTC)
    grant = db.scalar(
        select(PasswordResetGrant)
        .where(PasswordResetGrant.token_hash == hash_token(payload.reset_token))
        .with_for_update()
    )
    if (
        not grant
        or grant.consumed_at is not None
        or grant.revoked_at is not None
        or grant.expires_at.replace(tzinfo=UTC) <= now
    ):
        raise HTTPException(
            status_code=400, detail="Invalid or expired password reset authorization"
        )

    user = db.get(User, grant.user_id)
    if not user or not user.is_verified:
        raise HTTPException(
            status_code=400, detail="Invalid or expired password reset authorization"
        )

    user.password_hash = hash_password(payload.new_password)
    grant.consumed_at = now
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id,
        RefreshToken.revoked_at.is_(None),
    ).update({RefreshToken.revoked_at: now}, synchronize_session=False)
    db.commit()
    return {"message": "Password has been reset. Please log in with your new password."}
