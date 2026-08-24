from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, COMPANIES, INTERVIEW_REPORTS, INTERVIEW_SESSIONS, QUESTIONS, STUDENTS, TRANSCRIPTS
from app.db.mongodb import get_database
from app.models.interview_report import (
    DIFFICULTIES,
    CATEGORY_ALIASES,
    QUESTION_CATEGORIES,
    QUESTION_TYPE_ALIASES,
    QUESTION_TYPES,
    normalize_question_type,
)
from app.services.interview_persistence_service import (
    persist_candidate_report,
    persist_company_expectations,
    persist_questions,
)
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def _object_id(value: str, label: str):
    try:
        return to_object_id(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {label}") from exc


def _name_key(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def normalize_manual_category(value: str | None) -> str:
    """Reuse existing categories, otherwise create a safe manual-only slug."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Question category must be a non-empty string",
        )

    raw = value.strip().lower().replace("-", "_")
    existing_alias = CATEGORY_ALIASES.get(raw) or CATEGORY_ALIASES.get(raw.replace("_", " "))
    if raw in QUESTION_CATEGORIES:
        return raw
    if existing_alias in QUESTION_CATEGORIES:
        return existing_alias

    slug = raw.replace(" ", "_")
    slug = "".join(character if character.isalnum() or character == "_" else "_" for character in slug)
    slug = "_".join(part for part in slug.split("_") if part)
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Unsupported category "{value}"',
        )
    return slug


def normalize_manual_question_type(value: str | None) -> str:
    """Reuse existing question types, otherwise create a safe manual-only slug."""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Question type must be a non-empty string",
        )

    raw = value.strip().lower().replace("-", "_")
    alias = QUESTION_TYPE_ALIASES.get(raw) or QUESTION_TYPE_ALIASES.get(raw.replace("_", " "))
    if raw in QUESTION_TYPES:
        return raw
    if alias in QUESTION_TYPES:
        return alias

    slug = raw.replace(" ", "_")
    slug = "".join(character if character.isalnum() or character == "_" else "_" for character in slug)
    slug = "_".join(part for part in slug.split("_") if part)
    if not slug:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'Unsupported question type "{value}"',
        )
    return slug


async def _confirmed_participants(db, session: dict) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    session_students = {
        str(item.get("student_id")): item
        for item in session.get("students", [])
        if item.get("student_id")
    }
    transcript = await db[TRANSCRIPTS].find_one({"session_id": session["_id"]}, {"speaker_map": 1})
    speaker_map = {
        str(item.get("student_id")): item
        for item in (transcript or {}).get("speaker_map", [])
        if item.get("role") == "student" and item.get("student_id")
    }
    student_ids = [_object_id(student_id, "student id") for student_id in session_students]
    students = await db[STUDENTS].find({"_id": {"$in": student_ids}}, {"name": 1}).to_list(length=None)
    participants: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    for student in students:
        student_key = str(student["_id"])
        participant = {
            "student_id": student["_id"],
            "application_id": session_students[student_key].get("application_id"),
            "name": student.get("name") or "Student",
            "speaker_label": (speaker_map.get(student_key) or {}).get("speaker_label"),
        }
        participants[student_key] = participant
        by_name.setdefault(_name_key(participant["name"]), []).append(participant)
        if participant["speaker_label"]:
            by_name.setdefault(_name_key(participant["speaker_label"]), []).append(participant)
    return participants, by_name


def _resolve_candidate(candidate: Any, by_name: dict[str, list[dict]]) -> dict | None:
    matches = []
    for value in (candidate.candidate_name, candidate.speaker_label):
        if value:
            matches.extend(by_name.get(_name_key(value), []))
    unique_matches = {str(item["student_id"]): item for item in matches}
    return next(iter(unique_matches.values())) if len(unique_matches) == 1 else None


async def _validate_manual_payload(db, session_id: str, payload: Any) -> tuple[dict, dict[str, dict], list[dict], dict[str, dict]]:
    session_object_id = _object_id(session_id, "session id")
    session = await db[INTERVIEW_SESSIONS].find_one({"_id": session_object_id})
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview session not found")

    participants, by_name = await _confirmed_participants(db, session)
    if not participants:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This interview session has no confirmed participants")

    applications = await db[APPLICATIONS].find(
        {"_id": {"$in": [item.get("application_id") for item in participants.values()]},
         "student_id": {"$in": [item["student_id"] for item in participants.values()]},
         "opportunity_id": session.get("opportunity_id")},
        {"_id": 1, "student_id": 1, "company_id": 1, "opportunity_id": 1},
    ).to_list(length=None)
    application_by_student = {str(item["student_id"]): item for item in applications}

    if len(application_by_student) != len(participants):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A confirmed participant has no matching application for this opportunity")

    resolved_reports = {}
    for candidate in payload.candidates:
        participant = _resolve_candidate(candidate, by_name)
        if participant:
            resolved_reports[id(candidate)] = participant
    if len({str(item["student_id"]) for item in resolved_reports.values()}) != len(resolved_reports):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Each confirmed participant may have only one candidate report")
    if not payload.candidates:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Provide exactly one report for every confirmed interview participant")

    for question in payload.questions:
        if question.candidate_name or question.speaker_label:
            if not _resolve_candidate(question, by_name):
                continue
        if not question.question_text.strip():
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question text is required")
        question.category = normalize_manual_category(question.category)
        if question.difficulty.strip().lower() not in DIFFICULTIES:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Question difficulty is invalid")
        question.question_type = normalize_manual_question_type(question.question_type)

    return session, participants, list(application_by_student.values()), by_name


async def preview_manual_analysis(session_id: str, payload: Any) -> dict:
    db = get_database()
    session, participants, applications, by_name = await _validate_manual_payload(db, session_id, payload)
    existing_reports = await db[INTERVIEW_REPORTS].find(
        {"session_id": session["_id"], "student_id": {"$in": [item["student_id"] for item in applications]}},
        {"student_id": 1},
    ).to_list(length=None)
    existing_by_student = {str(item["student_id"]) for item in existing_reports}
    question_counts: dict[str, int] = {}
    anonymous_question_count = 0
    category_preview: list[dict[str, str]] = []
    type_preview: list[dict[str, str]] = []
    for question in payload.questions:
        participant = _resolve_candidate(question, by_name)
        if (question.candidate_name or question.speaker_label) and not participant:
            anonymous_question_count += 1
            continue
        key = participant["name"] if participant else (question.candidate_name or question.speaker_label or "Unknown candidate")
        question_counts[key] = question_counts.get(key, 0) + 1
        category_preview.append({
            "input": question.category,
            "normalized": normalize_manual_category(question.category),
            "status": "Existing" if normalize_manual_category(question.category) in QUESTION_CATEGORIES else "New category",
        })
        type_preview.append({
            "input": question.question_type,
            "normalized": normalize_manual_question_type(question.question_type),
            "status": "Existing" if normalize_manual_question_type(question.question_type) in QUESTION_TYPES else "New type",
        })
    return {
        "session_id": session["_id"],
        "candidates": len(payload.candidates),
        "questions": len(payload.questions),
        "anonymous_questions": anonymous_question_count,
        "categories": category_preview,
        "question_types": type_preview,
        "company_expectations": bool(payload.company_expectations and payload.company_expectations.expectations.strip()),
        "candidate_preview": [
            {
                "name": (resolved := _resolve_candidate(candidate, by_name) or {}).get("name") or candidate.candidate_name or candidate.speaker_label or "Unknown candidate",
                "mapping_status": "Matched to confirmed student" if resolved else "No student mapping",
                "application": "Found" if resolved else "Not found",
                "report": ("Update" if str(resolved["student_id"]) in existing_by_student else "Create") if resolved else "Not created",
                "questions": question_counts.get((resolved or {}).get("name") or candidate.candidate_name or candidate.speaker_label or "Unknown candidate", 0),
            }
            for candidate in payload.candidates
        ],
    }


async def save_manual_analysis(session_id: str, payload: Any) -> dict:
    db = get_database()
    session, participants, applications, by_name = await _validate_manual_payload(db, session_id, payload)
    application_by_student = {str(item["student_id"]): item for item in applications}
    now = datetime.now(timezone.utc)
    question_key_ids: dict[str, Any] = {}
    questions_by_student: dict[str, tuple[str | None, list[dict]]] = {}
    for question in payload.questions:
        item = question.model_dump()
        item["_normalized_category"] = question.category
        item["_normalized_question_type"] = question.question_type
        participant = _resolve_candidate(question, by_name)
        key = str(participant["student_id"]) if participant else f"anonymous:{question.candidate_name or question.speaker_label or 'candidate'}"
        asked_to = participant["name"] if participant else question.candidate_name or question.speaker_label
        if key not in questions_by_student:
            questions_by_student[key] = (asked_to, [])
        questions_by_student[key][1].append(item)

    students = await db[STUDENTS].find(
        {"_id": {"$in": [item["student_id"] for item in applications]}}, {"name": 1}
    ).to_list(length=None)
    name_by_student = {str(item["_id"]): item.get("name") for item in students}
    anonymous_questions = 0
    for student_id, (asked_to, questions) in questions_by_student.items():
        if questions:
            if student_id.startswith("anonymous:"):
                anonymous_questions += len(questions)
            question_key_ids.update(await persist_questions(
                db,
                session=session,
                questions=questions,
                asked_to=asked_to if student_id.startswith("anonymous:") else name_by_student.get(student_id),
                now=now,
            ))

    report_ids = []
    reports_skipped = 0
    for candidate in payload.candidates:
        participant = _resolve_candidate(candidate, by_name)
        if not participant:
            reports_skipped += 1
            continue
        student_id = participant["student_id"]
        application = application_by_student[str(student_id)]
        report_data = candidate.report.model_dump()
        overall = report_data.pop("overall", {})
        report_data.update({
            "score": overall.get("score"),
            "verdict": overall.get("verdict"),
            "summary": overall.get("summary"),
        })
        report_ids.append(await persist_candidate_report(
            db,
            session_id=session["_id"],
            student_id=student_id,
            application_id=application["_id"],
            company_id=session.get("company_id"),
            opportunity_id=session.get("opportunity_id"),
            speaker_label=None,
            block=None,
            report=report_data,
            key_to_id=question_key_ids,
            ai_model=None,
            ai_provider="manual",
            transcript_truncated=False,
            interview_date=session.get("scheduled_at"),
            now=now,
        ))

    if payload.company_expectations:
        await persist_company_expectations(
            db,
            session_id=session["_id"],
            expectations=payload.company_expectations.expectations,
            focus=payload.company_expectations.focus,
            ai_model=None,
            now=now,
        )

    return serialize_mongo({
        "session_id": session["_id"],
        "status": "completed",
        "candidate_reports_saved": len(report_ids),
        "reports_skipped": reports_skipped,
        "questions_saved": len(question_key_ids),
        "anonymous_questions": anonymous_questions,
        "company_expectations_saved": bool(payload.company_expectations),
    })
