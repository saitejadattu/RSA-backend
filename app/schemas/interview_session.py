from datetime import datetime

from pydantic import BaseModel, Field


class InterviewSessionStudentCreate(BaseModel):
    student_id: str
    application_id: str
    attendance_status: str = "pending"
    result_status: str = "pending"
    notes: str | None = None


class InterviewSessionCreate(BaseModel):
    company_id: str
    opportunity_id: str
    round_name: str = Field(..., min_length=1, max_length=120)
    round_type: str | None = None
    students: list[InterviewSessionStudentCreate] = Field(default_factory=list)
    interview_link: str | None = None
    transcript_drive_link: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: str = "scheduled"
    processed: bool = False
    transcript_status: str = "not_uploaded"
    ai_status: str = "not_started"
    created_by: str | None = None


class InterviewSessionUpdate(BaseModel):
    round_name: str | None = None
    round_type: str | None = None
    interview_link: str | None = None
    transcript_drive_link: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: str | None = None
    processed: bool | None = None
    transcript_status: str | None = None
    ai_status: str | None = None


class InterviewSessionStudentUpdate(BaseModel):
    attendance_status: str | None = None
    result_status: str | None = None
    notes: str | None = None


class TranscriptUpdate(BaseModel):
    transcript_drive_link: str
    transcript_status: str = "uploaded"


class MarkProcessedRequest(BaseModel):
    processed: bool = True
    ai_status: str = "completed"
    transcript_status: str | None = None
