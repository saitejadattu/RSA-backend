from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class StudentResponse(BaseModel):
    id: str
    external_user_id: str | None = None
    name: str
    email: EmailStr | None = None
    phone: str
    stack: str | None = None
    resume_link: str | None = None
    is_password_set: bool
    force_password_reset: bool
    created_at: datetime
    updated_at: datetime


class StudentImportRequest(BaseModel):
    sheet_url: str | None = None


class StudentImportResponse(BaseModel):
    inserted: int
    updated: int
    skipped: int


class StudentCreate(BaseModel):
    external_user_id: str | None = None
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str = Field(..., min_length=6, max_length=20)
    stack: str | None = None
    resume_link: str | None = None


class StudentPlacementUpdate(BaseModel):
    """Placement outcome set by an administrator."""

    placed_status: bool
