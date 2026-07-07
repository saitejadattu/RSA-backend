from pydantic import BaseModel, Field


class StudentCheckRequest(BaseModel):
    identifier: str = Field(..., min_length=3, description="Registered phone number or email")


class StudentCheckResponse(BaseModel):
    exists: bool
    password_setup_required: bool = False
    force_password_reset: bool = False
    message: str


class LoginRequest(BaseModel):
    identifier: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    status: str
    access_token: str | None = None
    reset_token: str | None = None
    token_type: str = "bearer"
    message: str


class AdminLoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=1)


class AdminLoginResponse(BaseModel):
    status: str
    access_token: str
    token_type: str = "bearer"
    message: str


class SetPasswordRequest(BaseModel):
    reset_token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class SetPasswordResponse(BaseModel):
    message: str


class AdminResetPasswordRequest(BaseModel):
    identifier: str = Field(..., min_length=3)
    temp_password: str | None = Field(default=None, min_length=6, max_length=128)


class AdminResetPasswordResponse(BaseModel):
    message: str
    temp_password: str
