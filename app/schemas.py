from datetime import datetime
import base64
import binascii

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
    image_data: list[str] = Field(default_factory=list, max_length=1)

    @field_validator("image_urls")
    @classmethod
    def require_https_images(cls, value: list[HttpUrl]) -> list[HttpUrl]:
        if any(image.scheme != "https" for image in value):
            raise ValueError("Image URLs must use HTTPS")
        return value

    @field_validator("image_data")
    @classmethod
    def validate_inline_images(cls, value: list[str]) -> list[str]:
        allowed_headers = {"data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"}
        for image in value:
            if len(image) > 4_100_000 or "," not in image:
                raise ValueError("Inline image must be no larger than 3 MB")
            header, encoded = image.split(",", 1)
            if header not in allowed_headers:
                raise ValueError("Inline images must be JPEG, PNG, or WebP data URLs")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("Inline image data is not valid base64") from None
            if len(decoded) > 3_000_000:
                raise ValueError("Inline image must be no larger than 3 MB")
        return value


class ChatResponse(BaseModel):
    conversation_id: int
    reply: str
