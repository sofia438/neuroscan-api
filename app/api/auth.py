import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import current_user
from app.core.config import get_settings
from app.db import get_db
from app.models import EmailOTP, PasswordResetGrant, PasswordResetOTP, RefreshToken, User
from app.schemas import ForgotPasswordRequest, LoginRequest, RefreshRequest, RegisterRequest, ResendOTPRequest, ResetPasswordRequest, TokenResponse, UserResponse, VerifyEmailRequest, VerifyResetCodeRequest
from app.security import create_access_token, create_refresh_token, hash_otp, hash_password, hash_token, new_otp, verify_password
from app.services.email import send_password_reset_code, send_verification_code

router = APIRouter(prefix="/auth", tags=["authentication"])
logger = logging.getLogger(__name__)


async def issue_otp(user: User, db: Session) -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    record = db.scalar(select(EmailOTP).where(EmailOTP.user_id == user.id))
    if record and record.sent_at.replace(tzinfo=UTC) + timedelta(seconds=settings.otp_resend_seconds) > now:
        raise HTTPException(status_code=429, detail="Please wait before requesting another code")
    code = new_otp()
    if record is None:
        record = EmailOTP(user_id=user.id, code_hash=hash_otp(user.email, code), expires_at=now + timedelta(minutes=settings.otp_expiry_minutes), sent_at=now, attempts=0)
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
        logger.exception("Verification email delivery failed; registration remains pending (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Could not send verification email. Try again shortly.") from exc


def token_pair(user: User, db: Session) -> TokenResponse:
    refresh, expires = create_refresh_token(user.id)
    db.add(RefreshToken(user_id=user.id, token_hash=hash_token(refresh), expires_at=expires))
    db.commit()
    return TokenResponse(access_token=create_access_token(user.id), refresh_token=refresh, expires_in=get_settings().access_token_minutes * 60)


@router.post("/register", status_code=status.HTTP_202_ACCEPTED)
async def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    if user and user.is_verified:
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    if user is None:
        user = User(email=email, password_hash=hash_password(payload.password))
        db.add(user)
        db.commit()
        db.refresh(user)
    await issue_otp(user, db)
    return {"message": "If this address can be registered, a verification code has been sent."}


@router.post("/verify-email", response_model=TokenResponse)
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email))
    record = db.scalar(select(EmailOTP).where(EmailOTP.user_id == user.id)) if user else None
    if not user or not record:
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    now = datetime.now(UTC)
    if record.expires_at.replace(tzinfo=UTC) <= now or record.attempts >= get_settings().otp_max_attempts:
        db.delete(record)
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_otp(email, payload.code)):
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    user.is_verified = True
    db.delete(record)
    db.commit()
    return token_pair(user, db)


@router.post("/resend-code", status_code=status.HTTP_202_ACCEPTED)
async def resend_code(payload: ResendOTPRequest, db: Session = Depends(get_db)) -> dict[str, str]:
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
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token)))
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
def logout(payload: RefreshRequest, db: Session = Depends(get_db), user: User = Depends(current_user)) -> None:
    record = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(payload.refresh_token), RefreshToken.user_id == user.id))
    if record and not record.revoked_at:
        record.revoked_at = datetime.now(UTC)
        db.commit()


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)) -> User:
    return user


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    """Always return the same response so callers cannot discover registered emails."""
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email, User.is_verified.is_(True)))
    if not user:
        return {"message": "If an account exists for this address, a password reset code has been sent."}

    settings = get_settings()
    now = datetime.now(UTC)
    record = db.scalar(select(PasswordResetOTP).where(PasswordResetOTP.user_id == user.id).with_for_update())
    if record and record.sent_at.replace(tzinfo=UTC) + timedelta(seconds=settings.otp_resend_seconds) > now:
        return {"message": "If an account exists for this address, a password reset code has been sent."}

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
        logger.exception("Password reset email delivery failed (%s)", type(exc).__name__)
        db.delete(record)
        db.commit()
    return {"message": "If an account exists for this address, a password reset code has been sent."}


@router.post("/verify-reset-code")
def verify_reset_code(payload: VerifyResetCodeRequest, db: Session = Depends(get_db)) -> dict[str, int | str]:
    email = str(payload.email).lower()
    user = db.scalar(select(User).where(User.email == email, User.is_verified.is_(True)))
    record = (
        db.scalar(select(PasswordResetOTP).where(PasswordResetOTP.user_id == user.id).with_for_update())
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
        raise HTTPException(status_code=400, detail="Invalid or expired password reset code")

    record.attempts += 1
    if not hmac.compare_digest(record.code_hash, hash_otp(email, payload.code)):
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid or expired password reset code")

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
    return {"reset_token": raw_token, "expires_in": settings.password_reset_token_minutes * 60}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)) -> dict[str, str]:
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
        raise HTTPException(status_code=400, detail="Invalid or expired password reset authorization")

    user = db.get(User, grant.user_id)
    if not user or not user.is_verified:
        raise HTTPException(status_code=400, detail="Invalid or expired password reset authorization")

    user.password_hash = hash_password(payload.new_password)
    grant.consumed_at = now
    db.query(RefreshToken).filter(
        RefreshToken.user_id == user.id,
        RefreshToken.revoked_at.is_(None),
    ).update({RefreshToken.revoked_at: now}, synchronize_session=False)
    db.commit()
    return {"message": "Password has been reset. Please log in with your new password."}
