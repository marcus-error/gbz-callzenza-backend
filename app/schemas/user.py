from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = ""


class UserLogin(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str = ""
    role: str = "USER"
    business_id: str | None = None
    tenant_id: str | None = None
    permissions: dict | None = None

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    business_id: str | None = None
    tenant_id: str | None = None
    role: str | None = None


class OrganizationRegisterRequest(BaseModel):
    company_name: str = Field(min_length=2, max_length=150)
    slug: str | None = Field(default=None, max_length=100)
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(default="", max_length=100)
    email: EmailStr
    password: str = Field(min_length=8)
    phone: str | None = None
    country: str | None = None


class OrganizationRegisterResponse(BaseModel):
    message: str
    business_id: str
    slug: str
    email: str
    verification_required: bool = True
    verification_url: str | None = None


class CheckSlugResponse(BaseModel):
    available: bool
    slug: str
    message: str


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class InvitationDetailsResponse(BaseModel):
    email: str
    role: str
    business_name: str
    business_slug: str
    is_expired: bool


class AcceptInvitationRequest(BaseModel):
    token: str
    password: str = Field(min_length=8)
    full_name: str | None = None
