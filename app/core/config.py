from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "NeuroScan API"
    environment: str = "development"
    database_url: str = ""
    migration_database_url: str = ""
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 14
    otp_expiry_minutes: int = 10
    otp_resend_seconds: int = 60
    otp_max_attempts: int = 5
    password_reset_token_minutes: int = 10
    brevo_api_key: str = ""
    brevo_sender_email: str = ""
    brevo_sender_name: str = "NeuroScan"
    openrouter_api_key: str = ""
    openrouter_model: str = "meta-llama/llama-4-scout"
    openrouter_app_name: str = "NeuroScan"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/v1/auth/google/callback"
    frontend_url: str = "http://localhost:5173"
    google_state_ttl_seconds: int = 600
    google_handoff_ttl_seconds: int = 90
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    mri_storage_dir: str = "./storage/mri"
    mri_max_upload_bytes: int = 2147483648
    doctor_approval_required: bool = False

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    @field_validator("jwt_secret")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if value and len(value) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return value

    @field_validator("jwt_algorithm")
    @classmethod
    def validate_algorithm(cls, value: str) -> str:
        if value != "HS256":
            raise ValueError("JWT_ALGORITHM must be HS256")
        return value

    @field_validator(
        "access_token_minutes",
        "refresh_token_days",
        "otp_expiry_minutes",
        "otp_resend_seconds",
        "otp_max_attempts",
        "password_reset_token_minutes",
        "mri_max_upload_bytes",
        "google_state_ttl_seconds",
        "google_handoff_ttl_seconds",
    )
    @classmethod
    def validate_positive_limits(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Token, OTP, and upload limits must be positive")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
