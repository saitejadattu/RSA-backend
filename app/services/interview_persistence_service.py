from __future__ import annotations

from datetime import datetime
from typing import Any

from app.db.collections import INTERVIEW_REPORTS, INTERVIEW_SESSIONS, QUESTIONS
from app.models.interview_report import (
    clamp,
    fix_asr_terms,
    normalize_category,
    normalize_correctness,
    normalize_difficulty,
    normalize_question_type,
    normalize_verdict,
    question_key,
    looks_context_bound,
)


async def persist_questions(
    db,
    *,
    session: dict[str, Any],
    questions: list[dict[str, Any]],
    asked_to: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Upsert normalized questions using the existing session/question key."""
    key_to_id: dict[str, Any] = {}
    for item in questions:
        text = fix_asr_terms((item.get("question_text") or "").strip())
        raw = fix_asr_terms((item.get("raw_question_text") or "").strip())
        key = question_key(text)
        if not key or key in key_to_id:
            continue
        document = {
            "session_id": session["_id"],
            "company_id": session.get("company_id"),
            "opportunity_id": session.get("opportunity_id"),
            "question_text": text,
            "raw_question_text": raw or None,
            "question_key": key,
            "category": item.get("_normalized_category") or normalize_category(item.get("category")),
            "topic": (item.get("topic") or "").strip() or None,
            "difficulty": normalize_difficulty(item.get("difficulty")),
            "is_technical": bool(item.get("is_technical")),
            "question_type": item.get("_normalized_question_type") or normalize_question_type(item.get("question_type")),
            "is_reusable": (
                bool(item.get("is_reusable"))
                and bool(item.get("is_technical"))
                and not looks_context_bound(text)
            ),
            "model_answer": (item.get("model_answer") or "").strip() or None,
            "why_asked": (item.get("why_asked") or "").strip() or None,
            "prepare": [p.strip() for p in (
                [item["prepare"]] if isinstance(item.get("prepare"), str) else (item.get("prepare") or [])
            ) if (p or "").strip()][:5],
            "asked_to": asked_to,
            "segment_order": item.get("segment_order"),
            "updated_at": now,
        }
        result = await db[QUESTIONS].find_one_and_update(
            {"session_id": session["_id"], "question_key": key},
            {"$set": document, "$setOnInsert": {"created_at": now}},
            upsert=True,
            return_document=True,
        )
        key_to_id[key] = result["_id"]
    return key_to_id


async def persist_candidate_report(
    db,
    *,
    session_id: Any,
    student_id: Any,
    application_id: Any,
    company_id: Any,
    opportunity_id: Any,
    speaker_label: str | None,
    block: dict[str, Any] | None,
    report: dict[str, Any],
    key_to_id: dict[str, Any],
    ai_model: str | None,
    ai_provider: str | None,
    transcript_truncated: bool,
    interview_date: Any,
    now: datetime,
) -> Any:
    """Upsert one candidate report, preserving existing publication state."""
    normalized_report = report or {}
    document = {
        "session_id": session_id,
        "student_id": student_id,
        "application_id": application_id,
        "company_id": company_id,
        "opportunity_id": opportunity_id,
        "speaker_label": speaker_label,
        "block": block,
        "overall": {
            "score": clamp(normalized_report.get("score"), 0, 10),
            "verdict": normalize_verdict(normalized_report.get("verdict")),
            "summary": (normalized_report.get("summary") or "").strip() or None,
        },
        "answers": _build_report_answers(normalized_report, key_to_id),
        "strengths": [s.strip() for s in normalized_report.get("strengths", []) if (s or "").strip()],
        "improvements": [
            {
                "area": (imp.get("area") or "").strip() or None,
                "detail": (imp.get("detail") or "").strip() or None,
                "priority": (imp.get("priority") or "medium").strip().lower(),
            }
            for imp in normalized_report.get("improvements", [])
        ],
        "skill_ratings": {
            (rating.get("skill") or "").strip().lower(): clamp(rating.get("rating"), 0, 5)
            for rating in normalized_report.get("skill_ratings", [])
            if (rating.get("skill") or "").strip()
        } if isinstance(normalized_report.get("skill_ratings"), list) else normalized_report.get("skill_ratings") or {},
        "communication": {
            "clarity": clamp((normalized_report.get("communication") or {}).get("clarity"), 0, 5),
            "confidence": clamp((normalized_report.get("communication") or {}).get("confidence"), 0, 5),
            "notes": ((normalized_report.get("communication") or {}).get("notes") or "").strip() or None,
        },
        "interviewer_feedback": (normalized_report.get("interviewer_feedback") or "").strip() or None,
        "interviewer_satisfaction": (normalized_report.get("interviewer_satisfaction") or "").strip() or None,
        "coaching_note": (normalized_report.get("coaching_note") or "").strip() or None,
        "ai_model": ai_model,
        "ai_provider": ai_provider,
        "ai_status": "completed",
        "transcript_truncated": transcript_truncated,
        "interview_date": interview_date,
        "generated_at": now,
        "updated_at": now,
    }
    result = await db[INTERVIEW_REPORTS].find_one_and_update(
        {"session_id": session_id, "student_id": student_id},
        {"$set": document, "$setOnInsert": {"visible_to_student": False, "created_at": now}},
        upsert=True,
        return_document=True,
    )
    return result["_id"]


def _build_report_answers(report: dict[str, Any], key_to_id: dict[str, Any]) -> list[dict[str, Any]]:
    answers = []
    for answer in report.get("answers", []):
        text = fix_asr_terms((answer.get("question_text") or "").strip())
        key = question_key(text)
        correctness = normalize_correctness(answer.get("correctness"))
        accuracy = clamp(answer.get("accuracy"), 0, 100)
        if correctness == "not_answered":
            accuracy = 0.0
        answers.append({
            "question_id": key_to_id.get(key) or answer.get("question_id"),
            "question_text": text,
            "student_answer": (answer.get("student_answer") or "").strip() or None,
            "accuracy": accuracy,
            "correctness": correctness,
            "feedback": (answer.get("feedback") or "").strip() or None,
            "ideal_answer": (answer.get("ideal_answer") or "").strip() or None,
        })
    return answers


async def persist_company_expectations(
    db,
    *,
    session_id: Any,
    expectations: str | None,
    focus: list[str],
    ai_model: str | None,
    now: datetime,
) -> None:
    """Store the existing session-level company expectations structure."""
    await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_id},
        {"$set": {"company_expectations": {
            "expectations": (expectations or "").strip() or None,
            "focus": [item.strip() for item in (focus or []) if (item or "").strip()],
            "generated_at": now,
            "ai_model": ai_model,
        }}},
    )
