from datetime import datetime

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


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


class StudentIssueCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=5000)
    category: Literal["BUG", "APPLICATION", "INTERVIEW", "FEEDBACK", "OTHER"]

    @field_validator("title", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value
