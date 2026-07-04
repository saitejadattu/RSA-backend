from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, INTERVIEW_SESSIONS
from app.db.mongodb import get_database
from app.schemas.interview_session import (
    InterviewSessionCreate,
    InterviewSessionStudentUpdate,
    InterviewSessionUpdate,
    MarkProcessedRequest,
    TranscriptUpdate,
)
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def object_id_or_422(value: str, label: str):
    try:
        return to_object_id(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {label}")


async def validate_session_references(payload: InterviewSessionCreate) -> tuple:
    db = get_database()
    company_id = object_id_or_422(payload.company_id, "company id")
    opportunity_id = object_id_or_422(payload.opportunity_id, "opportunity id")

    company = await db[COMPANIES].find_one({"_id": company_id})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": opportunity_id, "company_id": company_id})
    if not opportunity:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hiring opportunity not found")

    students = []
    seen_applications = set()
    for student in payload.students:
        student_id = object_id_or_422(student.student_id, "student id")
        application_id = object_id_or_422(student.application_id, "application id")
        if application_id in seen_applications:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Duplicate application in session")
        seen_applications.add(application_id)

        application = await db[APPLICATIONS].find_one(
            {
                "_id": application_id,
                "student_id": student_id,
                "company_id": company_id,
                "opportunity_id": opportunity_id,
            }
        )
        if not application:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Application does not match student/company/opportunity",
            )
        students.append(
            {
                "student_id": student_id,
                "application_id": application_id,
                "attendance_status": student.attendance_status,
                "result_status": student.result_status,
                "notes": student.notes,
            }
        )
    return company_id, opportunity_id, students


async def create_interview_session(payload: InterviewSessionCreate) -> dict:
    db = get_database()
    company_id, opportunity_id, students = await validate_session_references(payload)
    now = datetime.now(timezone.utc)
    document = {
        "company_id": company_id,
        "opportunity_id": opportunity_id,
        "round_name": payload.round_name,
        "round_type": payload.round_type,
        "students": students,
        "interview_link": payload.interview_link,
        "transcript_drive_link": payload.transcript_drive_link,
        "scheduled_at": payload.scheduled_at,
        "started_at": payload.started_at,
        "ended_at": payload.ended_at,
        "status": payload.status,
        "processed": payload.processed,
        "transcript_status": payload.transcript_status,
        "ai_status": payload.ai_status,
        "created_by": object_id_or_422(payload.created_by, "created_by") if payload.created_by else None,
        "created_at": now,
        "updated_at": now,
    }
    result = await db[INTERVIEW_SESSIONS].insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_mongo(document)


async def list_interview_sessions(
    *,
    company_id: str | None = None,
    opportunity_id: str | None = None,
    student_id: str | None = None,
    status_value: str | None = None,
    processed: bool | None = None,
    limit: int = 100,
) -> list[dict]:
    db = get_database()
    filters = {}
    if company_id:
        filters["company_id"] = object_id_or_422(company_id, "company id")
    if opportunity_id:
        filters["opportunity_id"] = object_id_or_422(opportunity_id, "opportunity id")
    if student_id:
        filters["students.student_id"] = object_id_or_422(student_id, "student id")
    if status_value:
        filters["status"] = status_value
    if processed is not None:
        filters["processed"] = processed

    sessions = await db[INTERVIEW_SESSIONS].find(filters).sort("scheduled_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(sessions)


async def get_interview_session(session_id: str) -> dict:
    db = get_database()
    object_id = object_id_or_422(session_id, "session id")
    session = await db[INTERVIEW_SESSIONS].find_one({"_id": object_id})
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview session not found")
    return serialize_mongo(session)


async def update_interview_session(session_id: str, payload: InterviewSessionUpdate) -> dict:
    db = get_database()
    object_id = object_id_or_422(session_id, "session id")
    update_fields = {key: value for key, value in payload.model_dump().items() if value is not None}
    if not update_fields:
        return await get_interview_session(session_id)
    update_fields["updated_at"] = datetime.now(timezone.utc)
    result = await db[INTERVIEW_SESSIONS].update_one({"_id": object_id}, {"$set": update_fields})
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview session not found")
    return await get_interview_session(session_id)


async def update_session_student(session_id: str, student_id: str, payload: InterviewSessionStudentUpdate) -> dict:
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    student_object_id = object_id_or_422(student_id, "student id")
    set_fields = {
        f"students.$.{key}": value
        for key, value in payload.model_dump().items()
        if value is not None
    }
    if not set_fields:
        return await get_interview_session(session_id)
    set_fields["updated_at"] = datetime.now(timezone.utc)
    result = await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_object_id, "students.student_id": student_object_id},
        {"$set": set_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session student not found")
    return await get_interview_session(session_id)


async def update_transcript(session_id: str, payload: TranscriptUpdate) -> dict:
    return await update_interview_session(
        session_id,
        InterviewSessionUpdate(
            transcript_drive_link=payload.transcript_drive_link,
            transcript_status=payload.transcript_status,
        ),
    )


async def mark_processed(session_id: str, payload: MarkProcessedRequest) -> dict:
    return await update_interview_session(
        session_id,
        InterviewSessionUpdate(
            processed=payload.processed,
            ai_status=payload.ai_status,
            transcript_status=payload.transcript_status,
        ),
    )
