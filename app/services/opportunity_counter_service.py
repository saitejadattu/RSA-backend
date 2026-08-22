from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.db.collections import APPLICATIONS, HIRING_OPPORTUNITIES
from app.db.mongodb import get_database
from app.utils.object_id import to_object_id


APPLICATION_COUNT_FILTER = {
    "$or": [
        {"application_details.interested": {"$exists": True, "$ne": False}},
        {
            "application_details": {"$exists": False},
            "is_interested": {"$ne": False},
            "status": {"$ne": "not_interested"},
        },
    ]
}

SHORTLIST_COUNT_FILTER = {
    "$or": [
        {"shortlist.is_shortlisted": True},
        {"screening.decision": "shortlisted"},
        {"current_status": "SHORTLISTED"},
        {"current_status": {"$exists": False}, "status": "shortlisted"},
    ]
}


def _status_value(application: dict[str, Any]) -> str | None:
    value = application.get("current_status")
    if value is not None:
        return str(value)
    return application.get("status")


def _application_is_real(application: dict[str, Any]) -> bool:
    details = application.get("application_details") or {}
    if isinstance(details, dict) and "interested" in details:
        return details.get("interested") is not False

    if application.get("is_interested") is False:
        return False

    status_value = _status_value(application)
    return str(status_value).lower() != "not_interested"


def _application_counted(application: dict[str, Any]) -> bool:
    return _application_is_real(application)


def _shortlist_counted(application: dict[str, Any]) -> bool:
    if not _application_is_real(application):
        return False

    shortlist = application.get("shortlist") or {}
    if isinstance(shortlist, dict) and shortlist.get("is_shortlisted") is True:
        return True

    screening = application.get("screening") or {}
    if isinstance(screening, dict) and screening.get("decision") == "shortlisted":
        return True

    status_value = _status_value(application)
    if status_value is None:
        return False
    return str(status_value).lower() in {"shortlisted"}


async def refresh_opportunity_counts(opportunity_id: str | Any) -> dict[str, int]:
    db = get_database()
    if isinstance(opportunity_id, str):
        try:
            object_id = to_object_id(opportunity_id)
        except ValueError:
            object_id = opportunity_id
    else:
        object_id = opportunity_id

    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": object_id}, {"_id": 1})
    if not opportunity:
        return {"application_count": 0, "shortlists_count": 0}

    applications = await db[APPLICATIONS].find({"opportunity_id": object_id}).to_list(length=None)

    application_count = 0
    shortlists_count = 0
    for application in applications:
        if _application_counted(application):
            application_count += 1
        if _shortlist_counted(application):
            shortlists_count += 1

    now = datetime.now(timezone.utc)
    await db[HIRING_OPPORTUNITIES].update_one(
        {"_id": object_id},
        {"$set": {"application_count": int(application_count), "shortlists_count": int(shortlists_count), "updated_at": now}},
    )
    return {"application_count": int(application_count), "shortlists_count": int(shortlists_count)}
