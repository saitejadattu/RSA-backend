from __future__ import annotations

import asyncio
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


async def counts_by_opportunity(opportunity_ids: list[Any]) -> dict[Any, dict[str, int]]:
    """Applied and shortlisted per opening, counted from the applications.

    The same numbers refresh_opportunity_counts stores, but read live. Screens
    that list many openings at once use this instead of the stored counters: a
    counter can be stale (it was blank on openings imported before counters
    existed, and a master import used to overwrite it), and a dashboard that
    says 0 next to a shortlist that exists is worse than a slightly slower one.
    """
    db = get_database()
    ids = list(opportunity_ids)
    if not ids:
        return {}

    async def grouped(match: dict) -> dict[Any, int]:
        rows = await db[APPLICATIONS].aggregate([
            {"$match": match},
            {"$group": {"_id": "$opportunity_id", "n": {"$sum": 1}}},
        ]).to_list(length=None)
        return {row["_id"]: row["n"] for row in rows}

    # Both reuse the filters above, so these can never drift from the stored counts.
    applied, shortlisted = await asyncio.gather(
        grouped({"opportunity_id": {"$in": ids}, **APPLICATION_COUNT_FILTER}),
        grouped({"$and": [{"opportunity_id": {"$in": ids}}, APPLICATION_COUNT_FILTER, SHORTLIST_COUNT_FILTER]}),
    )
    return {
        opportunity_id: {
            "application_count": applied.get(opportunity_id, 0),
            "shortlists_count": shortlisted.get(opportunity_id, 0),
        }
        for opportunity_id in ids
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


def is_shortlisted_application(application: dict[str, Any]) -> bool:
    """The one definition of "this person is shortlisted".

    Shared with the admin screens so a card and the stored counter can never
    disagree about the same application.
    """
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
        if is_shortlisted_application(application):
            shortlists_count += 1

    now = datetime.now(timezone.utc)
    await db[HIRING_OPPORTUNITIES].update_one(
        {"_id": object_id},
        {"$set": {"application_count": int(application_count), "shortlists_count": int(shortlists_count), "updated_at": now}},
    )
    return {"application_count": int(application_count), "shortlists_count": int(shortlists_count)}
