import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def _send_transactional_email(email: str, subject: str, body: str) -> None:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("BREVO_API_KEY", settings.brevo_api_key),
            ("BREVO_SENDER_EMAIL", settings.brevo_sender_email),
        )
        if not value.strip()
    ]
    if missing:
        logger.error("Transactional email unavailable: missing configuration %s", ", ".join(missing))
        raise RuntimeError("Brevo is not configured")
    payload = {
        "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
        "to": [{"email": email}],
        "subject": subject,
        "textContent": body,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": settings.brevo_api_key, "accept": "application/json"},
                json=payload,
            )
            if response.is_error:
                # Brevo's error body can include recipient details; don't log it.
                logger.error("Brevo rejected transactional email (HTTP %s)", response.status_code)
                response.raise_for_status()
            logger.info("Brevo accepted transactional email (HTTP %s)", response.status_code)
    except httpx.TimeoutException:
        logger.exception("Timed out while sending transactional email through Brevo")
        raise
    except httpx.HTTPStatusError:
        raise
    except httpx.RequestError as exc:
        logger.error("Could not reach Brevo while sending transactional email (%s)", type(exc).__name__)
        raise


async def send_verification_code(email: str, code: str) -> None:
    settings = get_settings()
    await _send_transactional_email(
        email,
        "Your NeuroScan verification code",
        f"Your NeuroScan verification code is {code}. It expires in {settings.otp_expiry_minutes} minutes.",
    )


async def send_password_reset_code(email: str, code: str) -> None:
    settings = get_settings()
    await _send_transactional_email(
        email,
        "Your NeuroScan password reset code",
        f"Your NeuroScan password reset code is {code}. It expires in {settings.otp_expiry_minutes} minutes. If you did not request this, you can ignore this email.",
    )
