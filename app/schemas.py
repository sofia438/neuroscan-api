from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, HttpUrl, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class ResendOTPRequest(BaseModel):
    email: EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: EmailStr
    is_verified: bool
    created_at: datetime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: int | None = None
    image_urls: list[HttpUrl] = Field(default_factory=list, max_length=4)

    @field_validator("image_urls")
    @classmethod
    def require_https_images(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        if any(image.scheme != "https" for image in value):
            raise ValueError("Image URLs must use HTTPS")
        return value


class ChatResponse(BaseModel):
    conversation_id: int
    reply: str
