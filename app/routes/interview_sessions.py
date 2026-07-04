from fastapi import APIRouter, Query

from app.schemas.interview_session import (
    InterviewSessionCreate,
    InterviewSessionStudentUpdate,
    InterviewSessionUpdate,
    MarkProcessedRequest,
    TranscriptUpdate,
)
from app.services.interview_session_service import (
    create_interview_session,
    get_interview_session,
    list_interview_sessions,
    mark_processed,
    update_interview_session,
    update_session_student,
    update_transcript,
)


router = APIRouter(prefix="/interview-sessions", tags=["Interview Sessions"])


@router.post("/")
async def create_session(payload: InterviewSessionCreate) -> dict:
    return await create_interview_session(payload)


@router.get("/")
async def get_sessions(
    company_id: str | None = None,
    opportunity_id: str | None = None,
    student_id: str | None = None,
    status: str | None = None,
    processed: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await list_interview_sessions(
        company_id=company_id,
        opportunity_id=opportunity_id,
        student_id=student_id,
        status_value=status,
        processed=processed,
        limit=limit,
    )


@router.get("/{session_id}")
async def get_session(session_id: str) -> dict:
    return await get_interview_session(session_id)


@router.patch("/{session_id}")
async def update_session(session_id: str, payload: InterviewSessionUpdate) -> dict:
    return await update_interview_session(session_id, payload)


@router.patch("/{session_id}/students/{student_id}")
async def update_student(session_id: str, student_id: str, payload: InterviewSessionStudentUpdate) -> dict:
    return await update_session_student(session_id, student_id, payload)


@router.post("/{session_id}/transcript")
async def upload_transcript_link(session_id: str, payload: TranscriptUpdate) -> dict:
    return await update_transcript(session_id, payload)


@router.post("/{session_id}/mark-processed")
async def process_marker(session_id: str, payload: MarkProcessedRequest) -> dict:
    return await mark_processed(session_id, payload)
