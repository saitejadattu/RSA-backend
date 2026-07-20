from fastapi import APIRouter, Depends, Query

from app.schemas.interview_report import (
    SpeakerMapUpdate,
    TranscriptConfirmRequest,
    TranscriptProposeRequest,
    TranscriptTextUpload,
)
from app.schemas.interview_session import (
    InterviewSessionCreate,
    InterviewSessionStudentUpdate,
    InterviewSessionUpdate,
    MarkProcessedRequest,
    TranscriptUpdate,
)
from app.services.interview_report_service import (
    analyze_session,
    confirm_transcript,
    get_transcript,
    list_session_reports,
    propose_from_transcript,
    save_transcript,
    update_speaker_map,
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
from app.utils.dependencies import require_admin_access


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


# --- RSA report pipeline (admin only: transcripts and AI feedback are sensitive) ---


@router.post("/transcript/propose", dependencies=[Depends(require_admin_access)])
async def propose_transcript(payload: TranscriptProposeRequest) -> dict:
    """Read a pasted transcript and propose the session plan: company, opening,
    interviewer, speaker->student mapping and per-candidate blocks.

    Writes nothing - review the proposal, then POST /transcript/confirm.
    When the opening is ambiguous, pick one from opportunity_options and call
    again with opportunity_id to get the speaker mapping.
    """
    return await propose_from_transcript(payload.raw_text, payload.opportunity_id)


@router.post("/transcript/confirm", dependencies=[Depends(require_admin_access)])
async def confirm_transcript_route(payload: TranscriptConfirmRequest) -> dict:
    """Commit a reviewed proposal: creates the interview session and stores the
    parsed transcript against it."""
    return await confirm_transcript(
        raw_text=payload.raw_text,
        company_id=payload.company_id,
        opportunity_id=payload.opportunity_id,
        speaker_map=[entry.model_dump() for entry in payload.speaker_map],
        round_name=payload.round_name,
        round_type=payload.round_type,
        source=payload.source,
    )


@router.post("/{session_id}/transcript/text", dependencies=[Depends(require_admin_access)])
async def upload_transcript_text(session_id: str, payload: TranscriptTextUpload) -> dict:
    """Paste/upload the raw transcript. Parses speakers and auto-maps them to students."""
    return await save_transcript(session_id, raw_text=payload.raw_text, source=payload.source)


@router.get("/{session_id}/transcript", dependencies=[Depends(require_admin_access)])
async def read_transcript(session_id: str) -> dict:
    return await get_transcript(session_id)


@router.patch("/{session_id}/speaker-map", dependencies=[Depends(require_admin_access)])
async def fix_speaker_map(session_id: str, payload: SpeakerMapUpdate) -> dict:
    """Correct the auto speaker->student mapping before running analysis."""
    return await update_speaker_map(session_id, [entry.model_dump() for entry in payload.speaker_map])


@router.post("/{session_id}/analyze", dependencies=[Depends(require_admin_access)])
async def run_analysis(session_id: str) -> dict:
    """Extract the question bank and generate one RSA report per student."""
    return await analyze_session(session_id)


@router.get("/{session_id}/reports", dependencies=[Depends(require_admin_access)])
async def session_reports(session_id: str) -> list[dict]:
    return await list_session_reports(session_id)
