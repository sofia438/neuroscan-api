import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def send_verification_code(email: str, code: str) -> None:
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
        logger.error("Verification email unavailable: missing configuration %s", ", ".join(missing))
        raise RuntimeError("Brevo is not configured")
    payload = {
        "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
        "to": [{"email": email}],
        "subject": "Your NeuroScan verification code",
        "textContent": f"Your NeuroScan verification code is {code}. It expires in {settings.otp_expiry_minutes} minutes.",
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
                logger.error("Brevo rejected verification email (HTTP %s)", response.status_code)
                response.raise_for_status()
            logger.info("Brevo accepted verification email (HTTP %s)", response.status_code)
    except httpx.TimeoutException:
        logger.exception("Timed out while sending verification email through Brevo")
        raise
    except httpx.HTTPStatusError:
        raise
    except httpx.RequestError as exc:
        logger.error("Could not reach Brevo while sending verification email (%s)", type(exc).__name__)
        raise
