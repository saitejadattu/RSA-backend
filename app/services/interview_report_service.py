import difflib
import re
from typing import Any

from fastapi import HTTPException, status

from app.db.collections import (
    APPLICATIONS,
    COMPANIES,
    HIRING_OPPORTUNITIES,
    INTERVIEW_REPORTS,
    INTERVIEW_SESSIONS,
    QUESTIONS,
    STATUS_HISTORY,
    STUDENTS,
    TRANSCRIPTS,
)
from app.db.mongodb import get_database
from app.models.interview_report import (
    clamp,
    fix_asr_terms,
    looks_context_bound,
    normalize_category,
    normalize_correctness,
    normalize_difficulty,
    normalize_question_type,
    normalize_verdict,
    question_key,
    utc_now,
)
from app.services.ai_service import analyze_candidate_block
from app.services.transcript_service import (
    build_speaker_map,
    candidate_blocks,
    detect_interviewer,
    distinct_speakers,
    parse_header,
    parse_transcript,
    transcript_to_text,
)
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def object_id_or_422(value: str, label: str):
    try:
        return to_object_id(value)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid {label}")


async def _load_session(db, session_object_id) -> dict:
    session = await db[INTERVIEW_SESSIONS].find_one({"_id": session_object_id})
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview session not found")
    return session


async def _session_students(db, session: dict) -> list[dict[str, Any]]:
    """Session students enriched with their name, for speaker matching."""
    student_ids = [entry["student_id"] for entry in session.get("students", []) if entry.get("student_id")]
    if not student_ids:
        return []
    students = await db[STUDENTS].find({"_id": {"$in": student_ids}}, {"name": 1}).to_list(length=None)
    by_id = {student["_id"]: student.get("name") for student in students}
    return [
        {
            "student_id": entry["student_id"],
            "application_id": entry.get("application_id"),
            "name": by_id.get(entry["student_id"]),
        }
        for entry in session.get("students", [])
        if entry.get("student_id")
    ]


# --- proposal: turn a pasted transcript into a reviewable session plan --------


def _company_key(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    parts = [part for part in text.split("-") if part]
    while parts and parts[-1] in {"pvt", "ltd", "private", "limited", "llp", "india", "inc"}:
        parts.pop()
    return "-".join(parts)


async def _match_company(db, company_hint: str | None) -> dict | None:
    if not company_hint:
        return None
    key = _company_key(company_hint)
    exact = await db[COMPANIES].find_one({"company_key": key})
    if exact:
        return exact

    companies = await db[COMPANIES].find({}, {"name": 1, "company_key": 1, "aliases": 1}).to_list(length=None)
    scored: list[tuple[float, dict]] = []
    for company in companies:
        names = [company.get("name"), company.get("company_key"), *(company.get("aliases") or [])]
        best = max(
            (difflib.SequenceMatcher(None, key, _company_key(name)).ratio() for name in names if name),
            default=0.0,
        )
        if best >= 0.85:
            scored.append((best, company))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1] if scored else None


async def _match_opportunity(db, company: dict, meeting_date) -> tuple[dict | None, list[dict]]:
    """Pick the opening this interview belongs to.

    Interviews happen after the opening lands, so among a company's openings the
    best fit is the most recent one received on/before the meeting date.
    Ambiguity is returned rather than guessed.
    """
    opportunities = await db[HIRING_OPPORTUNITIES].find({"company_id": company["_id"]}).to_list(length=None)
    if not opportunities:
        return None, []
    if len(opportunities) == 1:
        return opportunities[0], opportunities

    if meeting_date:
        prior = [
            opportunity for opportunity in opportunities
            if opportunity.get("opportunity_received_at")
            and opportunity["opportunity_received_at"].replace(tzinfo=meeting_date.tzinfo) <= meeting_date
        ]
        if prior:
            prior.sort(key=lambda item: item["opportunity_received_at"], reverse=True)
            newest = prior[0]["opportunity_received_at"]
            # A company often posts several openings on the same day (WeSee has
            # both "Full Stack Intern" and "AI Intern" on 9-Jun-2026). Picking
            # the first of a tie would silently attach the interview to the
            # wrong opening, so surface the tie instead of guessing.
            tied = [item for item in prior if item["opportunity_received_at"] == newest]
            if len(tied) == 1:
                return tied[0], opportunities
            return None, tied
    return None, opportunities


async def _shortlisted_students(db, opportunity: dict) -> list[dict[str, Any]]:
    """The candidates we expect in the room: applications shortlisted (or beyond)
    for this opening."""
    applications = await db[APPLICATIONS].find(
        {
            "opportunity_id": opportunity["_id"],
            "current_status": {
                "$in": [
                    "SHORTLISTED",
                    "INTERVIEW_SCHEDULED",
                    "INTERVIEW_IN_PROGRESS",
                    "SELECTED",
                    "OFFER_PENDING",
                    "OFFER_RELEASED",
                    "OFFER_ACCEPTED",
                    "JOINED",
                ]
            },
        },
        {"student_id": 1},
    ).to_list(length=None)
    if not applications:
        return []
    student_ids = [application["student_id"] for application in applications]
    students = await db[STUDENTS].find({"_id": {"$in": student_ids}}, {"name": 1, "email": 1}).to_list(length=None)
    name_by_id = {student["_id"]: student for student in students}
    return [
        {
            "student_id": application["student_id"],
            "application_id": application["_id"],
            "name": (name_by_id.get(application["student_id"]) or {}).get("name"),
            "email": (name_by_id.get(application["student_id"]) or {}).get("email"),
        }
        for application in applications
        if application["student_id"] in name_by_id
    ]


async def propose_from_transcript(raw_text: str, opportunity_id: str | None = None) -> dict:
    """Read a pasted transcript and propose the whole session plan WITHOUT
    writing anything: which company/opening it belongs to, who the interviewer
    is, which shortlisted student each speaker maps to, and where each
    candidate's interview starts and ends. The admin confirms before any write.

    Pass opportunity_id to resolve an ambiguous opening (a company often posts
    several roles on the same day). Without it we cannot know whose shortlist to
    match speakers against, so the caller re-proposes once the admin picks.
    """
    db = get_database()
    if not (raw_text or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Transcript text is empty")

    header = parse_header(raw_text)
    segments = parse_transcript(raw_text)
    if not segments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No speaker-separated lines found. Expected lines like 'Name: what they said'.",
        )

    speakers = distinct_speakers(segments)
    company = await _match_company(db, header.get("company_hint"))
    opportunity, opportunity_options = (None, [])
    shortlisted: list[dict[str, Any]] = []

    if opportunity_id:
        chosen_id = object_id_or_422(opportunity_id, "opportunity id")
        opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": chosen_id})
        if not opportunity:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        company = await db[COMPANIES].find_one({"_id": opportunity["company_id"]})
    elif company:
        opportunity, opportunity_options = await _match_opportunity(db, company, header.get("meeting_date"))

    if opportunity:
        shortlisted = await _shortlisted_students(db, opportunity)

    speaker_map = build_speaker_map(speakers, shortlisted)
    student_speakers = {item["speaker_label"] for item in speaker_map if item["role"] == "student"}
    interviewer = detect_interviewer(segments, student_speakers)
    for item in speaker_map:
        if item["speaker_label"] == interviewer and item["role"] != "student":
            item["role"] = "interviewer"
            item["confidence"] = 1.0

    blocks = candidate_blocks(segments, sorted(student_speakers), interviewer)
    matched_student_ids = {str(item["student_id"]) for item in speaker_map if item.get("student_id")}

    return serialize_mongo(
        {
            "header": {
                "title": header.get("title"),
                "meeting_date": header.get("meeting_date"),
                "company_hint": header.get("company_hint"),
            },
            "company": {"id": company["_id"], "name": company.get("name")} if company else None,
            "opportunity": (
                {"id": opportunity["_id"], "role": opportunity.get("role"),
                 "received_on": opportunity.get("opportunity_received_on")}
                if opportunity else None
            ),
            "opportunity_options": [
                {"id": item["_id"], "role": item.get("role"), "received_on": item.get("opportunity_received_on")}
                for item in opportunity_options
            ] if not opportunity else [],
            "interviewer": interviewer,
            "segment_count": len(segments),
            "speaker_map": speaker_map,
            "blocks": [
                {
                    "speaker_label": block["speaker_label"],
                    "start_order": block["start_order"],
                    "end_order": block["end_order"],
                    "segment_count": block["segment_count"],
                }
                for block in blocks
            ],
            "shortlisted_students": shortlisted,
            # Nothing is silently dropped: both directions of mismatch surface.
            "unmatched_speakers": [
                item["speaker_label"] for item in speaker_map
                if item["role"] == "unknown"
            ],
            "missing_students": [
                {"student_id": student["student_id"], "name": student.get("name")}
                for student in shortlisted
                if str(student["student_id"]) not in matched_student_ids
            ],
            "warnings": _proposal_warnings(company, opportunity, shortlisted, speaker_map, interviewer),
        }
    )


def _proposal_warnings(company, opportunity, shortlisted, speaker_map, interviewer) -> list[str]:
    warnings: list[str] = []
    if not company:
        warnings.append("Could not match a company from the transcript title. Pick one manually.")
    elif not opportunity:
        warnings.append(
            "Company matched but the opening is ambiguous. Pick one from opportunity_options and "
            "call propose again with opportunity_id to map speakers."
        )
    elif not shortlisted:
        warnings.append("This opening has no shortlisted applications, so no students could be matched.")
    if not interviewer:
        warnings.append("Could not identify the interviewer.")
    if not any(item["role"] == "student" for item in speaker_map):
        warnings.append("No speaker mapped to a student. Fix the speaker map before analysis.")
    low = [item["speaker_label"] for item in speaker_map if item["role"] == "student" and item["confidence"] < 0.8]
    if low:
        warnings.append(f"Low-confidence student matches, please verify: {', '.join(low)}")
    return warnings


async def confirm_transcript(
    *,
    raw_text: str,
    company_id: str,
    opportunity_id: str,
    speaker_map: list[dict[str, Any]],
    round_name: str = "Interview",
    round_type: str | None = None,
    source: str = "paste",
) -> dict:
    """Commit an admin-reviewed proposal: create the interview session from the
    confirmed speaker map and store the parsed transcript against it."""
    db = get_database()
    company_object_id = object_id_or_422(company_id, "company id")
    opportunity_object_id = object_id_or_422(opportunity_id, "opportunity id")

    company = await db[COMPANIES].find_one({"_id": company_object_id})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    opportunity = await db[HIRING_OPPORTUNITIES].find_one(
        {"_id": opportunity_object_id, "company_id": company_object_id}
    )
    if not opportunity:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found for this company")

    header = parse_header(raw_text)
    segments = parse_transcript(raw_text)
    if not segments:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Transcript has no speaker lines")

    # Resolve each confirmed student speaker to its application for this opening.
    resolved_map: list[dict[str, Any]] = []
    session_students: list[dict[str, Any]] = []
    seen_students: set[str] = set()
    for item in speaker_map:
        label = (item.get("speaker_label") or "").strip()
        if not label:
            continue
        role = item.get("role") or "unknown"
        student_object_id = None
        if item.get("student_id"):
            student_object_id = object_id_or_422(item["student_id"], "student id")
            if str(student_object_id) in seen_students:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Student mapped to more than one speaker: {label}",
                )
            application = await db[APPLICATIONS].find_one(
                {"student_id": student_object_id, "opportunity_id": opportunity_object_id},
                {"_id": 1},
            )
            if not application:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"'{label}' has no application for this opportunity",
                )
            seen_students.add(str(student_object_id))
            role = "student"
            session_students.append(
                {
                    "student_id": student_object_id,
                    "application_id": application["_id"],
                    "attendance_status": "attended",
                    "result_status": "pending",
                    "notes": None,
                }
            )
        resolved_map.append(
            {"speaker_label": label, "student_id": student_object_id, "role": role, "confidence": 1.0}
        )

    if not session_students:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Confirm at least one speaker as a student before saving.",
        )

    now = utc_now()
    interviewer = next((item["speaker_label"] for item in resolved_map if item["role"] == "interviewer"), None)
    student_speakers = [item["speaker_label"] for item in resolved_map if item["role"] == "student"]
    blocks = candidate_blocks(segments, student_speakers, interviewer)
    label_to_student = {item["speaker_label"]: item["student_id"] for item in resolved_map if item["student_id"]}

    session_document = {
        "company_id": company_object_id,
        "opportunity_id": opportunity_object_id,
        "round_name": round_name,
        "round_type": round_type,
        "students": session_students,
        "interview_link": None,
        "transcript_drive_link": None,
        "scheduled_at": header.get("meeting_date"),
        "started_at": header.get("meeting_date"),
        "ended_at": None,
        "status": "completed",
        "processed": False,
        "transcript_status": "uploaded",
        "ai_status": "not_started",
        "source": "transcript_import",
        "created_by": None,
        "created_at": now,
        "updated_at": now,
    }
    session_result = await db[INTERVIEW_SESSIONS].insert_one(session_document)
    session_object_id = session_result.inserted_id

    transcript_document = {
        "session_id": session_object_id,
        "company_id": company_object_id,
        "opportunity_id": opportunity_object_id,
        "source": source,
        "source_link": None,
        "title": header.get("title"),
        "meeting_date": header.get("meeting_date"),
        "company_hint": header.get("company_hint"),
        "raw_text": raw_text,
        "segments": segments,
        "speaker_map": resolved_map,
        "interviewer": interviewer,
        "blocks": [
            {
                "speaker_label": block["speaker_label"],
                "student_id": label_to_student.get(block["speaker_label"]),
                "start_order": block["start_order"],
                "end_order": block["end_order"],
                "segment_count": block["segment_count"],
            }
            for block in blocks
        ],
        "created_at": now,
        "updated_at": now,
    }
    await db[TRANSCRIPTS].insert_one(transcript_document)

    return serialize_mongo(
        {
            "session_id": session_object_id,
            "company": {"id": company_object_id, "name": company.get("name")},
            "opportunity": {"id": opportunity_object_id, "role": opportunity.get("role")},
            "interviewer": interviewer,
            "students_confirmed": len(session_students),
            "blocks": transcript_document["blocks"],
            "segment_count": len(segments),
        }
    )


async def save_transcript(session_id: str, *, raw_text: str, source: str = "paste") -> dict:
    """Parse a pasted/uploaded transcript, auto-map speakers to the session's
    students, and store it. Re-uploading replaces the stored transcript."""
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    session = await _load_session(db, session_object_id)

    if not (raw_text or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Transcript text is empty")

    segments = parse_transcript(raw_text)
    if not segments:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No speaker-separated lines found. Expected lines like 'Name: what they said'.",
        )

    students = await _session_students(db, session)
    speakers = distinct_speakers(segments)
    speaker_map = build_speaker_map(speakers, students)
    now = utc_now()

    document = {
        "session_id": session_object_id,
        "company_id": session.get("company_id"),
        "opportunity_id": session.get("opportunity_id"),
        "source": source,
        "source_link": session.get("transcript_drive_link"),
        "raw_text": raw_text,
        "segments": segments,
        "speaker_map": speaker_map,
        "updated_at": now,
    }
    existing = await db[TRANSCRIPTS].find_one({"session_id": session_object_id}, {"_id": 1})
    if existing:
        await db[TRANSCRIPTS].update_one({"_id": existing["_id"]}, {"$set": document})
        transcript_id = existing["_id"]
    else:
        document["created_at"] = now
        result = await db[TRANSCRIPTS].insert_one(document)
        transcript_id = result.inserted_id

    await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_object_id},
        {"$set": {"transcript_status": "uploaded", "ai_status": "not_started", "updated_at": now}},
    )

    return serialize_mongo(
        {
            "_id": transcript_id,
            "session_id": session_object_id,
            "segment_count": len(segments),
            "speakers": speakers,
            "speaker_map": speaker_map,
            "unmapped_speakers": [item["speaker_label"] for item in speaker_map if item["role"] == "unknown"],
        }
    )


async def get_transcript(session_id: str) -> dict:
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    transcript = await db[TRANSCRIPTS].find_one({"session_id": session_object_id})
    if not transcript:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No transcript for this session")
    return serialize_mongo(transcript)


async def update_speaker_map(session_id: str, mapping: list[dict[str, Any]]) -> dict:
    """Admin correction of the auto speaker->student mapping before analysis."""
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    session = await _load_session(db, session_object_id)
    transcript = await db[TRANSCRIPTS].find_one({"session_id": session_object_id})
    if not transcript:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No transcript for this session")

    valid_student_ids = {entry["student_id"] for entry in session.get("students", [])}
    known_speakers = {item["speaker_label"] for item in transcript.get("speaker_map", [])}

    rebuilt: list[dict[str, Any]] = []
    for item in mapping:
        label = item.get("speaker_label")
        if label not in known_speakers:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown speaker label: {label}",
            )
        role = item.get("role") or "unknown"
        student_id = None
        if item.get("student_id"):
            student_id = object_id_or_422(item["student_id"], "student id")
            if student_id not in valid_student_ids:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="student_id is not part of this session",
                )
            role = "student"
        rebuilt.append(
            {"speaker_label": label, "student_id": student_id, "role": role, "confidence": 1.0}
        )

    await db[TRANSCRIPTS].update_one(
        {"_id": transcript["_id"]},
        {"$set": {"speaker_map": rebuilt, "updated_at": utc_now()}},
    )
    return serialize_mongo({"session_id": session_object_id, "speaker_map": rebuilt})


async def _persist_questions(db, *, session, questions: list[dict], asked_to: str | None, now) -> dict[str, Any]:
    """Upsert each extracted question, keyed by (session, question_key) so a
    re-run replaces rather than duplicates. Returns question_key -> _id."""
    key_to_id: dict[str, Any] = {}
    for item in questions:
        # question_text is the AI's rewritten, standalone form; raw_question_text
        # is what was actually said. The bank shows the rewrite.
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
            "category": normalize_category(item.get("category")),
            "topic": (item.get("topic") or "").strip() or None,
            "difficulty": normalize_difficulty(item.get("difficulty")),
            "is_technical": bool(item.get("is_technical")),
            "question_type": normalize_question_type(item.get("question_type")),
            # Gate for the student practice bank: a follow-up like "Which one?"
            # is a real question but useless to anyone who wasn't in the room.
            # Three gates, all must pass to reach the student practice bank:
            # the model's own judgement, "is it technical", and a hard check
            # that the wording isn't tied to this room. The model reliably
            # rewrites grammar but still lets "Can you show me your tool..."
            # through as reusable, so the last gate is not redundant.
            "is_reusable": (
                bool(item.get("is_reusable"))
                and bool(item.get("is_technical"))
                and not looks_context_bound(text)
            ),
            "model_answer": (item.get("model_answer") or "").strip() or None,
            "why_asked": (item.get("why_asked") or "").strip() or None,
            "prepare": [p.strip() for p in (item.get("prepare") or []) if (p or "").strip()][:5],
            # NOTE: asked_to holds a student's name. It must never be returned
            # to students - see student_practice_questions().
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


def _build_report_answers(report: dict, key_to_id: dict[str, Any]) -> list[dict[str, Any]]:
    answers = []
    for answer in report.get("answers", []):
        text = fix_asr_terms((answer.get("question_text") or "").strip())
        key = question_key(text)
        correctness = normalize_correctness(answer.get("correctness"))
        accuracy = clamp(answer.get("accuracy"), 0, 100)
        # A question the candidate never answered must not carry an accuracy
        # score - otherwise "not answered" reads as a partially correct answer.
        if correctness == "not_answered":
            accuracy = 0.0
        answers.append(
            {
                "question_id": key_to_id.get(key),
                "question_text": text,
                "student_answer": (answer.get("student_answer") or "").strip() or None,
                "accuracy": accuracy,
                "correctness": correctness,
                "feedback": (answer.get("feedback") or "").strip() or None,
                "ideal_answer": (answer.get("ideal_answer") or "").strip() or None,
            }
        )
    return answers


# Statuses that sit BEFORE the interview: analysing a transcript proves the
# interview happened, so these advance. Anything further along (SELECTED,
# OFFER_*, JOINED) or already closed (REJECTED, DROPPED) is left alone - a
# re-run must never drag someone who already has an offer back to "in progress".
ADVANCEABLE_STATUSES = {"APPLIED", "PROFILE_SHARED", "SHORTLISTED", "INTERVIEW_SCHEDULED"}

# Their own vocabulary (see hiring_opportunities.company_status). Only an
# undecided opening moves; a decided one (Hired / Not Hired / Drop / cv
# rejected) must not be reopened by uploading a transcript.
OPPORTUNITY_IN_PROGRESS = "Hiring-in-progress"
OPPORTUNITY_OPEN_STATUSES = {None, "", "Yet To Schedule Interviews"}


async def _advance_after_interview(db, *, session, student_ids: list, now) -> dict[str, Any]:
    """Move interviewed students forward and mark the opening as in progress."""
    advanced: list[str] = []
    for student_id in student_ids:
        application = await db[APPLICATIONS].find_one(
            {"student_id": student_id, "opportunity_id": session.get("opportunity_id")},
            {"current_status": 1, "student_id": 1, "company_id": 1, "opportunity_id": 1},
        )
        if not application:
            continue
        old_status = application.get("current_status")
        if old_status not in ADVANCEABLE_STATUSES:
            continue

        await db[APPLICATIONS].update_one(
            {"_id": application["_id"]},
            {"$set": {"current_status": "INTERVIEW_IN_PROGRESS", "updated_at": now}},
        )
        await db[STATUS_HISTORY].insert_one(
            {
                "application_id": application["_id"],
                "student_id": application["student_id"],
                "company_id": application.get("company_id"),
                "opportunity_id": application.get("opportunity_id"),
                "old_status": old_status,
                "new_status": "INTERVIEW_IN_PROGRESS",
                "reason": "Interview transcript analysed",
                "notes": None,
                "changed_by": None,
                "changed_by_role": "system",
                "source": "interview_analysis",
                "created_at": now,
            }
        )
        advanced.append(str(application["_id"]))

    opportunity_updated = False
    opportunity = await db[HIRING_OPPORTUNITIES].find_one(
        {"_id": session.get("opportunity_id")}, {"company_status": 1}
    )
    if opportunity and (opportunity.get("company_status") in OPPORTUNITY_OPEN_STATUSES):
        await db[HIRING_OPPORTUNITIES].update_one(
            {"_id": opportunity["_id"]},
            {"$set": {"company_status": OPPORTUNITY_IN_PROGRESS, "updated_at": now}},
        )
        opportunity_updated = True

    return {"applications_advanced": len(advanced), "opportunity_status_updated": opportunity_updated}


async def analyze_session(session_id: str) -> dict:
    """Run AI analysis over the stored transcript, then persist the extracted
    question bank and one report per student. Reports start unpublished."""
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    session = await _load_session(db, session_object_id)
    transcript = await db[TRANSCRIPTS].find_one({"session_id": session_object_id})
    if not transcript:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Upload a transcript before running analysis.",
        )

    speaker_map = transcript.get("speaker_map") or []
    student_speakers = {
        item["speaker_label"]: item["student_id"]
        for item in speaker_map
        if item.get("role") == "student" and item.get("student_id")
    }
    if not student_speakers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No transcript speaker is mapped to a student. Fix the speaker map first.",
        )

    company = await db[COMPANIES].find_one({"_id": session.get("company_id")}, {"name": 1})
    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": session.get("opportunity_id")}, {"role": 1})

    segments = transcript.get("segments") or []
    interviewer = transcript.get("interviewer") or detect_interviewer(segments, set(student_speakers))
    blocks = candidate_blocks(segments, list(student_speakers.keys()), interviewer)
    if not blocks:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No candidate blocks found in transcript.")

    await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_object_id}, {"$set": {"ai_status": "in_progress", "updated_at": utc_now()}}
    )

    # Re-analysing must replace this session's questions, not add to them. The
    # model rephrases slightly between runs, so a changed question_key would
    # upsert a near-duplicate instead of updating the original and the bank
    # would grow on every re-run.
    await db[QUESTIONS].delete_many({"session_id": session_object_id})

    application_by_student = {
        entry["student_id"]: entry.get("application_id") for entry in session.get("students", [])
    }
    context = {
        "company": (company or {}).get("name"),
        "role": (opportunity or {}).get("role"),
        "round_name": session.get("round_name"),
        "interviewer": interviewer,
    }

    written: list[Any] = []
    all_question_keys: set[str] = set()
    failures: list[dict[str, str]] = []
    model_used: str | None = None

    # One call per candidate: focused context, and one student's answers can
    # never leak into another's report.
    for block in blocks:
        label = block["speaker_label"]
        student_id = student_speakers.get(label)
        if not student_id:
            continue
        try:
            analysis = await analyze_candidate_block(
                transcript_text=transcript_to_text(block["segments"]),
                student_label=label,
                context=context,
            )
        except HTTPException as exc:
            failures.append({"speaker_label": label, "detail": str(exc.detail)})
            continue

        now = utc_now()
        model_used = analysis.get("_model")
        key_to_id = await _persist_questions(
            db, session=session, questions=analysis.get("questions", []), asked_to=label, now=now
        )
        all_question_keys.update(key_to_id.keys())

        report = analysis.get("report") or {}
        document = {
            "session_id": session_object_id,
            "student_id": student_id,
            "application_id": application_by_student.get(student_id),
            "company_id": session.get("company_id"),
            "opportunity_id": session.get("opportunity_id"),
            "speaker_label": label,
            "block": {"start_order": block["start_order"], "end_order": block["end_order"]},
            "overall": {
                "score": clamp(report.get("score"), 0, 10),
                "verdict": normalize_verdict(report.get("verdict")),
                "summary": (report.get("summary") or "").strip() or None,
            },
            "answers": _build_report_answers(report, key_to_id),
            "strengths": [s.strip() for s in report.get("strengths", []) if (s or "").strip()],
            "improvements": [
                {
                    "area": (imp.get("area") or "").strip() or None,
                    "detail": (imp.get("detail") or "").strip() or None,
                    "priority": (imp.get("priority") or "medium").strip().lower(),
                }
                for imp in report.get("improvements", [])
            ],
            "skill_ratings": {
                (rating.get("skill") or "").strip().lower(): clamp(rating.get("rating"), 0, 5)
                for rating in report.get("skill_ratings", [])
                if (rating.get("skill") or "").strip()
            },
            "communication": {
                "clarity": clamp((report.get("communication") or {}).get("clarity"), 0, 5),
                "confidence": clamp((report.get("communication") or {}).get("confidence"), 0, 5),
                "notes": ((report.get("communication") or {}).get("notes") or "").strip() or None,
            },
            # The interviewer's own words, kept separate from anything the model
            # generated - it is the most trustworthy feedback in the room.
            "interviewer_feedback": (report.get("interviewer_feedback") or "").strip() or None,
            "ai_model": analysis.get("_model"),
            "ai_provider": analysis.get("_provider"),
            "ai_status": "completed",
            "transcript_truncated": bool(analysis.get("_truncated")),
            "generated_at": now,
            "updated_at": now,
        }

        result = await db[INTERVIEW_REPORTS].find_one_and_update(
            {"session_id": session_object_id, "student_id": student_id},
            {
                "$set": document,
                # Never silently re-publish: a re-run keeps the existing gate,
                # and a brand-new report starts hidden from the student.
                "$setOnInsert": {"visible_to_student": False, "created_at": now},
            },
            upsert=True,
            return_document=True,
        )
        written.append(result["_id"])

    final_status = "completed" if written and not failures else ("failed" if not written else "partial")
    await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_object_id},
        {"$set": {"ai_status": final_status, "processed": bool(written), "updated_at": utc_now()}},
    )
    if not written:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Analysis failed for every candidate: {failures}",
        )

    # The interview demonstrably happened, so move the pipeline forward.
    advanced = await _advance_after_interview(
        db,
        session=session,
        student_ids=[student_speakers[block["speaker_label"]] for block in blocks
                     if student_speakers.get(block["speaker_label"])],
        now=utc_now(),
    )

    return {
        "session_id": str(session_object_id),
        "status": final_status,
        "candidates_analyzed": len(written),
        "questions_extracted": len(all_question_keys),
        "students": [block["speaker_label"] for block in blocks],
        "model": model_used,
        "failures": failures,
        **advanced,
    }


async def list_session_reports(session_id: str) -> list[dict]:
    db = get_database()
    session_object_id = object_id_or_422(session_id, "session id")
    reports = await db[INTERVIEW_REPORTS].find({"session_id": session_object_id}).to_list(length=None)
    student_ids = [report["student_id"] for report in reports]
    students = await db[STUDENTS].find({"_id": {"$in": student_ids}}, {"name": 1, "email": 1}).to_list(length=None)
    by_id = {student["_id"]: student for student in students}
    for report in reports:
        student = by_id.get(report["student_id"]) or {}
        report["student"] = {"id": student.get("_id"), "name": student.get("name"), "email": student.get("email")}
    return serialize_mongo(reports)


async def set_report_visibility(report_id: str, visible: bool) -> dict:
    db = get_database()
    report_object_id = object_id_or_422(report_id, "report id")
    result = await db[INTERVIEW_REPORTS].find_one_and_update(
        {"_id": report_object_id},
        {"$set": {"visible_to_student": visible, "updated_at": utc_now()}},
        return_document=True,
    )
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return serialize_mongo(result)


async def list_student_reports(student_id) -> list[dict]:
    """Only published reports are ever returned to a student."""
    db = get_database()
    reports = await db[INTERVIEW_REPORTS].find(
        {"student_id": student_id, "visible_to_student": True}
    ).sort("generated_at", -1).to_list(length=None)

    company_ids = [report.get("company_id") for report in reports if report.get("company_id")]
    opportunity_ids = [report.get("opportunity_id") for report in reports if report.get("opportunity_id")]
    companies = await db[COMPANIES].find({"_id": {"$in": company_ids}}, {"name": 1}).to_list(length=None)
    opportunities = await db[HIRING_OPPORTUNITIES].find(
        {"_id": {"$in": opportunity_ids}}, {"role": 1}
    ).to_list(length=None)
    company_by_id = {item["_id"]: item.get("name") for item in companies}
    opportunity_by_id = {item["_id"]: item.get("role") for item in opportunities}

    for report in reports:
        report["company"] = {"id": report.get("company_id"), "name": company_by_id.get(report.get("company_id"))}
        report["opportunity"] = {
            "id": report.get("opportunity_id"),
            "role": opportunity_by_id.get(report.get("opportunity_id")),
        }
        report.pop("speaker_label", None)
        # A student gets coaching, not a hiring decision. The overall score and
        # strong/average/weak verdict read as a hire/reject signal and stay
        # internal to admins; the summary and per-question detail remain, which
        # is what actually helps them improve.
        overall = report.get("overall") or {}
        report["overall"] = {"summary": overall.get("summary")}
    return serialize_mongo(reports)


async def list_questions(
    *,
    company_id: str | None = None,
    opportunity_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    technical_only: bool = True,
    limit: int = 200,
) -> list[dict]:
    """Flat list of extracted questions, filterable for the company detail view."""
    db = get_database()
    filters: dict[str, Any] = {}
    if company_id:
        filters["company_id"] = object_id_or_422(company_id, "company id")
    if opportunity_id:
        filters["opportunity_id"] = object_id_or_422(opportunity_id, "opportunity id")
    if session_id:
        filters["session_id"] = object_id_or_422(session_id, "session id")
    if category:
        filters["category"] = normalize_category(category)
    if technical_only:
        filters["is_technical"] = True

    questions = await db[QUESTIONS].find(filters).sort("created_at", -1).limit(limit).to_list(length=limit)
    return serialize_mongo(questions)


async def student_practice_questions(
    *,
    include_scenario: bool = False,
    category: str | None = None,
    difficulty: str | None = None,
    search: str | None = None,
    limit: int = 300,
) -> dict:
    """The practice bank every student can see.

    Deliberately narrow: only technical questions that stand on their own, and
    NEVER anything personal. `asked_to` holds a real student's name and answers
    live in interview_reports - none of that is projected here, so one student
    can never learn what another was asked or how they scored.

    Scenario questions are excluded unless explicitly requested.
    """
    db = get_database()
    match: dict[str, Any] = {"is_technical": True, "is_reusable": True}
    if not include_scenario:
        match["question_type"] = {"$ne": "scenario"}
    if category:
        match["category"] = normalize_category(category)
    if difficulty:
        match["difficulty"] = normalize_difficulty(difficulty)
    if search and search.strip():
        match["question_text"] = {"$regex": re.escape(search.strip()), "$options": "i"}

    pipeline: list[dict[str, Any]] = [
        {"$match": match},
        {
            "$group": {
                "_id": "$question_key",
                "question_text": {"$first": "$question_text"},
                "category": {"$first": "$category"},
                "question_type": {"$first": "$question_type"},
                "difficulty": {"$first": "$difficulty"},
                "topic": {"$first": "$topic"},
                # A model answer written for the subject, not for any student.
                "model_answer": {"$max": "$model_answer"},
                "why_asked": {"$max": "$why_asked"},
                "prepare": {"$first": "$prepare"},
                "times_asked": {"$sum": 1},
                "company_ids": {"$addToSet": "$company_id"},
                "last_asked_at": {"$max": "$created_at"},
            }
        },
        {"$sort": {"times_asked": -1, "last_asked_at": -1}},
        {"$limit": limit},
        {"$lookup": {"from": COMPANIES, "localField": "company_ids", "foreignField": "_id", "as": "companies"}},
        {
            # Whitelist the output. asked_to / student answers / scores are not
            # listed here and therefore cannot leak.
            "$project": {
                "_id": 0,
                "question_key": "$_id",
                "question_text": 1,
                "category": 1,
                "question_type": 1,
                "difficulty": 1,
                "topic": 1,
                "model_answer": 1,
                "why_asked": 1,
                "prepare": 1,
                "times_asked": 1,
                "companies": {"$map": {"input": "$companies", "as": "c", "in": "$$c.name"}},
            }
        },
    ]
    questions = serialize_mongo(await db[QUESTIONS].aggregate(pipeline).to_list(length=limit))

    # Group by topic so a student revises one area at a time rather than
    # bouncing between Docker and RAG down a flat list.
    grouped: dict[str, list[dict]] = {}
    for question in questions:
        grouped.setdefault(question.get("category") or "other", []).append(question)
    groups = [
        {"category": category, "count": len(items), "questions": items}
        for category, items in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]

    facets = await db[QUESTIONS].aggregate(
        [
            {"$match": {"is_technical": True, "is_reusable": True}},
            {
                "$group": {
                    "_id": {"category": "$category", "question_type": "$question_type"},
                    "n": {"$sum": 1},
                }
            },
        ]
    ).to_list(length=None)
    categories = sorted({row["_id"]["category"] for row in facets if row["_id"].get("category")})
    scenario_count = sum(row["n"] for row in facets if row["_id"].get("question_type") == "scenario")

    return {
        "questions": questions,
        "groups": groups,
        "total": len(questions),
        "categories": categories,
        "scenario_available": scenario_count,
        "include_scenario": include_scenario,
    }


async def company_interview_insights(company_id: str) -> dict:
    """What a company tends to ask - shared with every student.

    Company Focus is derived from the topics of that company's reusable
    questions rather than written by the model, so it can never contradict the
    questions actually listed underneath it.
    """
    db = get_database()
    company_object_id = object_id_or_422(company_id, "company id")
    company = await db[COMPANIES].find_one({"_id": company_object_id}, {"name": 1})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")

    rows = await db[QUESTIONS].aggregate(
        [
            {"$match": {"company_id": company_object_id, "is_technical": True, "is_reusable": True}},
            {
                "$group": {
                    "_id": "$question_key",
                    "question_text": {"$first": "$question_text"},
                    "category": {"$first": "$category"},
                    "topic": {"$first": "$topic"},
                    "question_type": {"$first": "$question_type"},
                    "difficulty": {"$first": "$difficulty"},
                    "model_answer": {"$max": "$model_answer"},
                    "why_asked": {"$max": "$why_asked"},
                    "prepare": {"$first": "$prepare"},
                    "times_asked": {"$sum": 1},
                }
            },
            {"$sort": {"times_asked": -1}},
            {
                "$project": {
                    "_id": 0,
                    "question_key": "$_id",
                    "question_text": 1, "category": 1, "topic": 1, "question_type": 1,
                    "difficulty": 1, "model_answer": 1, "why_asked": 1, "prepare": 1,
                    "times_asked": 1,
                }
            },
        ]
    ).to_list(length=None)

    focus: list[str] = []
    for row in rows:
        for value in (row.get("category"), row.get("topic")):
            label = (value or "").strip()
            if label and label not in focus:
                focus.append(label)

    return serialize_mongo(
        {
            "company": {"id": company_object_id, "name": company.get("name")},
            "focus": focus[:10],
            "questions": rows,
            "total": len(rows),
        }
    )


async def question_bank(*, technical_only: bool = True, limit: int = 200) -> list[dict]:
    """The deduplicated bank: same question asked across sessions collapses into
    one row with how often it came up and which companies asked it."""
    db = get_database()
    match: dict[str, Any] = {}
    if technical_only:
        match["is_technical"] = True

    pipeline: list[dict[str, Any]] = []
    if match:
        pipeline.append({"$match": match})
    pipeline += [
        {
            "$group": {
                "_id": "$question_key",
                "question_text": {"$first": "$question_text"},
                "category": {"$first": "$category"},
                "difficulty": {"$first": "$difficulty"},
                "topic": {"$first": "$topic"},
                "times_asked": {"$sum": 1},
                "company_ids": {"$addToSet": "$company_id"},
                "last_asked_at": {"$max": "$created_at"},
            }
        },
        {"$sort": {"times_asked": -1, "last_asked_at": -1}},
        {"$limit": limit},
        {"$lookup": {"from": COMPANIES, "localField": "company_ids", "foreignField": "_id", "as": "companies"}},
        {
            "$project": {
                "_id": 0,
                "question_key": "$_id",
                "question_text": 1,
                "category": 1,
                "difficulty": 1,
                "topic": 1,
                "times_asked": 1,
                "last_asked_at": 1,
                "companies": {
                    "$map": {"input": "$companies", "as": "c", "in": {"id": "$$c._id", "name": "$$c.name"}}
                },
            }
        },
    ]
    return serialize_mongo(await db[QUESTIONS].aggregate(pipeline).to_list(length=limit))
