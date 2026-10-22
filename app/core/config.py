from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "NeuroScan API"
    environment: str = "development"
    database_url: str = ""
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 14
    otp_expiry_minutes: int = 10
    otp_resend_seconds: int = 60
    otp_max_attempts: int = 5
    brevo_api_key: str = ""
    brevo_sender_email: str = ""
    brevo_sender_name: str = "NeuroScan"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-4-scout"
    openrouter_app_name: str = "NeuroScan"
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
