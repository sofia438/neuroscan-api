import httpx

from app.core.config import get_settings


async def send_verification_code(email: str, code: str) -> None:
    settings = get_settings()
    if not settings.brevo_api_key or not settings.brevo_sender_email:
        raise RuntimeError("Brevo is not configured")
    payload = {
        "sender": {"name": settings.brevo_sender_name, "email": settings.brevo_sender_email},
        "to": [{"email": email}],
        "subject": "Your NeuroScan verification code",
        "textContent": f"Your NeuroScan verification code is {code}. It expires in {settings.otp_expiry_minutes} minutes.",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post("https://api.brevo.com/v3/smtp/email", headers={"api-key": settings.brevo_api_key, "accept": "application/json"}, json=payload)
        response.raise_for_status()
