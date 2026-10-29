import base64
import binascii
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(default="", max_length=200)
    role: str = Field(default="patient", pattern=r"^(doctor|patient)$")
    city: str | None = Field(default=None, max_length=120)
    hospital: str | None = Field(default=None, max_length=200)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class ResendOTPRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class VerifyResetCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class ResetPasswordRequest(BaseModel):
    reset_token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class OAuthExchangeRequest(BaseModel):
    code: str = Field(min_length=32, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    is_verified: bool
    full_name: str
    role: str
    city: str | None
    hospital: str | None
    doctor_verified: bool
    has_tc: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user):
        return cls(
            id=user.id,
            email=user.email,
            is_verified=user.is_verified,
            full_name=user.full_name,
            role=user.role,
            city=user.city,
            hospital=user.hospital,
            doctor_verified=user.doctor_verified,
            has_tc=bool(user.patient_tc_hash),
            created_at=user.created_at,
        )


class ProfileUpdateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    hospital: str | None = Field(default=None, max_length=200)


class PatientIdRequest(BaseModel):
    patient_tc: str = Field(pattern=r"^[1-9]\d{10}$")


class CaseCreateRequest(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    patient_name: str = Field(min_length=1, max_length=200)
    age: int = Field(ge=0, le=120)
    sex: str = Field(min_length=1, max_length=30)
    scan_date: datetime
    condition: str = Field(default="tumor", pattern=r"^(tumor|alzheimer)$")
    patient_tc: str = Field(pattern=r"^[1-9]\d{10}$")


class VisitCreateRequest(BaseModel):
    visit_date: datetime
    notes: str | None = Field(default=None, max_length=10000)


class VisitUpdateRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=10000)
    status: str | None = Field(
        default=None, pattern=r"^(Pending|Processing|Done|Failed)$"
    )


class CaseUpdateRequest(BaseModel):
    status: str | None = Field(
        default=None, pattern=r"^(Pending|Processing|Done|Failed)$"
    )
    risk: str | None = Field(default=None, pattern=r"^(Low|Moderate|High)$")
    scan_date: date | None = None


class VisibilityUpdateRequest(BaseModel):
    scan_findings: bool = True
    growth_pattern: bool = True
    treatment_considerations: bool = True
    surgical_assessment: bool = False
    next_steps: bool = True
    tumor_type: bool = False
    survival_estimate: bool = False
    clinical_markers: bool = False
    xai_explanation: bool = False
    doctor_note: str = Field(default="", max_length=10000)


class AnalysisResultCreateRequest(BaseModel):
    tumor_type: str | None = Field(default=None, max_length=200)
    grade: str | None = Field(default=None, max_length=100)
    idh_status: str | None = Field(default=None, max_length=100)
    mgmt_status: str | None = Field(default=None, max_length=100)
    survival_estimate: str | None = Field(default=None, max_length=200)
    probabilities: dict[str, Annotated[float, Field(ge=0, le=100)]] | None = None
    xai_features: list["XaiFeature"] = Field(default_factory=list, max_length=100)


class XaiFeature(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    importance: float = Field(ge=0, le=100)


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
        allowed_headers = {
            "data:image/jpeg;base64",
            "data:image/png;base64",
            "data:image/webp;base64",
        }
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
