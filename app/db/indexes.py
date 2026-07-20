from pymongo import ASCENDING
from pymongo.errors import OperationFailure

from app.db.collections import (
    APPLICATIONS,
    COMPANIES,
    COMPANY_APPLICATIONS,
    COMPANY_SHORTLISTS,
    HIRING_OPPORTUNITIES,
    INTERVIEW_REPORTS,
    INTERVIEW_SESSIONS,
    QUESTIONS,
    STATUS_HISTORY,
    STUDENTS,
    TRANSCRIPTS,
)
from app.db.mongodb import get_database


async def create_indexes() -> None:
    db = get_database()
    try:
        await db[STUDENTS].drop_index("email_1")
    except OperationFailure:
        pass

    await db[STUDENTS].create_index(
        [("email", ASCENDING)],
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    await db[STUDENTS].create_index([("phone", ASCENDING)], unique=True)
    await db[STUDENTS].create_index(
        [("external_user_id", ASCENDING)],
        unique=True,
        partialFilterExpression={"external_user_id": {"$type": "string"}},
    )
    await db[STUDENTS].create_index([("stack", ASCENDING)])
    await db[STUDENTS].create_index([("created_at", ASCENDING)])

    for old_index in ("company_key_1_role_key_1", "company_key_1_role_key_1_opportunity_key_1"):
        try:
            await db[COMPANIES].drop_index(old_index)
        except OperationFailure:
            pass

    await db[COMPANIES].create_index([("name", ASCENDING)])
    await db[COMPANIES].create_index([("company_key", ASCENDING)], unique=True)
    await db[COMPANIES].create_index([("created_at", ASCENDING)])

    await db[COMPANY_APPLICATIONS].create_index(
        [("company_id", ASCENDING), ("student_uid", ASCENDING)],
        unique=True,
        partialFilterExpression={"student_uid": {"$type": "string"}},
    )
    await db[COMPANY_APPLICATIONS].create_index([("company_id", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("student_id", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("student_uid", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("is_interested", ASCENDING)])
    await db[COMPANY_APPLICATIONS].create_index([("status", ASCENDING)])

    await db[COMPANY_SHORTLISTS].create_index([("company_id", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index([("email", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index([("status", ASCENDING)])
    await db[COMPANY_SHORTLISTS].create_index(
        [("company_id", ASCENDING), ("email", ASCENDING)],
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )

    await db[HIRING_OPPORTUNITIES].create_index([("company_id", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("role_key", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("opportunity_received_at", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("company_status", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index(
        [("company_id", ASCENDING), ("role_key", ASCENDING), ("opportunity_key", ASCENDING)],
        unique=True,
    )

    await db[APPLICATIONS].create_index([("student_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("company_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("opportunity_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("current_status", ASCENDING)])
    await db[APPLICATIONS].create_index([("final_status", ASCENDING)])
    await db[APPLICATIONS].create_index([("application_details.interested", ASCENDING)])
    await db[APPLICATIONS].create_index(
        [("opportunity_id", ASCENDING), ("student_id", ASCENDING)],
        unique=True,
    )

    await db[INTERVIEW_SESSIONS].create_index([("company_id", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("opportunity_id", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("scheduled_at", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("status", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("processed", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("students.student_id", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index([("students.application_id", ASCENDING)])
    await db[INTERVIEW_SESSIONS].create_index(
        [("opportunity_id", ASCENDING), ("round_name", ASCENDING), ("scheduled_at", ASCENDING)]
    )

    await db[STATUS_HISTORY].create_index([("application_id", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("student_id", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("company_id", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("opportunity_id", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("new_status", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("created_at", ASCENDING)])
    await db[STATUS_HISTORY].create_index([("application_id", ASCENDING), ("created_at", ASCENDING)])

    # One transcript per interview session.
    await db[TRANSCRIPTS].create_index([("session_id", ASCENDING)], unique=True)
    await db[TRANSCRIPTS].create_index([("company_id", ASCENDING)])

    # A question is stored once per (session, question_key); question_key groups
    # the same question across sessions into the dedup'd bank.
    await db[QUESTIONS].create_index([("session_id", ASCENDING), ("question_key", ASCENDING)], unique=True)
    await db[QUESTIONS].create_index([("question_key", ASCENDING)])
    await db[QUESTIONS].create_index([("company_id", ASCENDING)])
    await db[QUESTIONS].create_index([("opportunity_id", ASCENDING)])
    await db[QUESTIONS].create_index([("category", ASCENDING)])
    await db[QUESTIONS].create_index([("is_technical", ASCENDING)])

    # One report per (session, student).
    await db[INTERVIEW_REPORTS].create_index([("session_id", ASCENDING), ("student_id", ASCENDING)], unique=True)
    await db[INTERVIEW_REPORTS].create_index([("student_id", ASCENDING), ("visible_to_student", ASCENDING)])
    await db[INTERVIEW_REPORTS].create_index([("company_id", ASCENDING)])
    await db[INTERVIEW_REPORTS].create_index([("opportunity_id", ASCENDING)])
    await db[INTERVIEW_REPORTS].create_index([("generated_at", ASCENDING)])
