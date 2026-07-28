from datetime import datetime, timezone
from typing import Any


APPLICATION_STATUSES = {
    "APPLIED",
    "PROFILE_SHARED",
    "SHORTLISTED",
    "NOT_SHORTLISTED",
    "INTERVIEW_SCHEDULED",
    "INTERVIEW_IN_PROGRESS",
    "SELECTED",
    "OFFER_PENDING",
    "OFFER_RELEASED",
    "OFFER_ACCEPTED",
    "OFFER_REJECTED",
    "JOINED",
    "REJECTED",
    "DROPPED",
}

FINAL_STATUSES = {"HIRED", "REJECTED", "DROPPED"}
OFFER_STATUSES = {"PENDING", "RELEASED", "ACCEPTED", "REJECTED"}
INTERNSHIP_STATUSES = {"YET_TO_START", "IN_PROGRESS", "COMPLETED", "DISCONTINUED", "TERMINATED"}

LEGACY_STATUS_MAP = {
    "applied": "APPLIED",
    "not_interested": "DROPPED",
    "shortlisted": "SHORTLISTED",
    "not_shortlisted": "NOT_SHORTLISTED",
    "interview_scheduled": "INTERVIEW_SCHEDULED",
    "in_progress": "INTERVIEW_IN_PROGRESS",
    "interview_in_progress": "INTERVIEW_IN_PROGRESS",
    "selected": "SELECTED",
    "offer_pending": "OFFER_PENDING",
    "offer_released": "OFFER_RELEASED",
    "offer_accepted": "OFFER_ACCEPTED",
    "offer_rejected": "OFFER_REJECTED",
    "joined": "JOINED",
    "rejected": "REJECTED",
    "hired": "SELECTED",
    "dropped": "DROPPED",
}

FINAL_STATUS_BY_CURRENT_STATUS = {
    "SELECTED": "HIRED",
    "JOINED": "HIRED",
    "REJECTED": "REJECTED",
    "DROPPED": "DROPPED",
}

SELF_ASSESSMENT_KEYS = ("python", "nodejs", "react", "mongodb", "sql", "dsa", "javascript")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"yes", "y", "true", "1", "interested", "available"}:
        return True
    if text in {"no", "n", "false", "0", "not interested", "not_interested", "unavailable"}:
        return False
    return None


def normalize_application_status(value: str | None, *, interested: bool | None = None) -> str:
    if not value:
        return "DROPPED" if interested is False else "APPLIED"
    normalized = value.strip().upper()
    if normalized in APPLICATION_STATUSES:
        return normalized
    return LEGACY_STATUS_MAP.get(value.strip().lower(), "DROPPED" if interested is False else "APPLIED")


def final_status_for(current_status: str, *, interested: bool | None = None) -> str | None:
    if interested is False and current_status == "DROPPED":
        return "DROPPED"
    return FINAL_STATUS_BY_CURRENT_STATUS.get(current_status)


def status_for_api(application: dict[str, Any]) -> str | None:
    return application.get("current_status") or application.get("status")


def interested_for_api(application: dict[str, Any]) -> bool | None:
    details = application.get("application_details") or {}
    if "interested" in details:
        return details.get("interested")
    return application.get("is_interested")


def is_real_application(application: dict[str, Any]) -> bool:
    return interested_for_api(application) is not False


def self_assessment_from_skills(skills: dict[str, Any] | None) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {}
    source = skills or {}
    aliases = {
        "node": "nodejs",
        "node_js": "nodejs",
        "nodejs": "nodejs",
        "mongo": "mongodb",
        "mongo_db": "mongodb",
        "mongodb": "mongodb",
        "js": "javascript",
        "javascript": "javascript",
        "python": "python",
        "react": "react",
        "sql": "sql",
        "dsa": "dsa",
    }
    for raw_key, raw_value in source.items():
        key = aliases.get(str(raw_key).strip().lower())
        if key not in SELF_ASSESSMENT_KEYS:
            continue
        value = raw_value.get("score") if isinstance(raw_value, dict) else raw_value
        if isinstance(value, (int, float)):
            result[key] = value
            continue
        try:
            result[key] = int(str(value).strip())
        except (TypeError, ValueError):
            result[key] = None
    return result


def default_placement() -> dict[str, Any]:
    return {
        "selected": False,
        "offer_letter": {"status": None, "url": None, "received_at": None},
        "internship": {
            "status": None,
            "joining_date": None,
            "stipend": None,
            "duration_months": None,
            "location": None,
        },
    }


def build_application_details(
    *,
    interested: bool | None,
    skills: dict[str, Any] | None = None,
    has_relevant_project_experience: Any = None,
    github_link: str | None = None,
    project_link: str | None = None,
    submitted_resume_url: str | None = None,
    willing_remote: Any = None,
    available_full_duration: Any = None,
    comfortable_stipend: Any = None,
    comfortable_schedule: Any = None,
    college_noc: Any = None,
    interest_reason: str | None = None,
    non_interest_reason: str | None = None,
    other_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "interested": interested,
        "non_interest_reason": non_interest_reason,
        "self_assessment": self_assessment_from_skills(skills),
        "has_relevant_project_experience": parse_bool(has_relevant_project_experience),
        "github_link": github_link,
        "project_link": project_link,
        "submitted_resume_url": submitted_resume_url,
        "willing_remote": parse_bool(willing_remote),
        "available_full_duration": parse_bool(available_full_duration),
        "comfortable_stipend": parse_bool(comfortable_stipend),
        "comfortable_schedule": parse_bool(comfortable_schedule),
        "college_noc": parse_bool(college_noc),
        "interest_reason": interest_reason,
        "other_response": other_response or {},
    }
