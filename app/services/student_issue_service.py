from datetime import datetime, timezone

from app.db.collections import STUDENT_ISSUES
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


def _display_status(value: str | None) -> str:
    return {"OPEN": "IN_PROGRESS", "RESOLVED": "CLOSED"}.get(value, value or "IN_PROGRESS")


def _reply_for_student(reply: dict | None) -> dict | None:
    """One admin reply as the student sees it: the answer and when it came.

    The stored reply also carries the admin's id and email - internal audit
    data that is no business of the student's.
    """
    if not reply or not (reply.get("message") or "").strip():
        return None
    return {
        "message": reply["message"],
        "responded_at": reply.get("responded_at"),
        "responded_by": (reply.get("responded_by") or {}).get("name") or "RSA team",
    }


def _updated_by_for_student(issue: dict) -> dict | None:
    """Who last touched the ticket, as the student may see it.

    Their own reopen keeps its full record; an admin's is reduced to a name,
    since the stored one carries that admin's id and email.
    """
    who = issue.get("updated_by")
    if not who:
        return who
    if issue.get("updated_by_type") == "STUDENT":
        return who
    return {"name": who.get("name") or "RSA team"}


def _student_issue(issue: dict) -> dict:
    shaped = {**issue, "status": _display_status(issue.get("status"))}
    # What the admin answered - the reason this ticket was closed.
    shaped["resolution"] = _reply_for_student(issue.get("resolution"))
    shaped["replies"] = [
        reply for reply in (_reply_for_student(item) for item in issue.get("resolution_history") or []) if reply
    ]
    shaped.pop("resolution_history", None)
    if "updated_by" in shaped:
        shaped["updated_by"] = _updated_by_for_student(issue)
    return shaped


async def create_student_issue(student: dict, payload) -> dict:
    now = datetime.now(timezone.utc)
    document = {
        "student_id": student["_id"],
        "title": payload.title,
        "description": payload.description,
        "category": payload.category,
        "status": "IN_PROGRESS",
        "created_at": now,
        "updated_at": now,
    }
    result = await get_database()[STUDENT_ISSUES].insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_mongo(_student_issue(document))


async def list_student_issues(student: dict) -> list[dict]:
    issues = await get_database()[STUDENT_ISSUES].find(
        {"student_id": student["_id"]}
    ).sort("created_at", -1).to_list(length=None)
    return serialize_mongo([_student_issue(issue) for issue in issues])


async def get_student_issue(student: dict, issue_id: str) -> dict:
    try:
        object_id = to_object_id(issue_id)
    except ValueError as exc:
        raise ValueError("Invalid issue id") from exc
    issue = await get_database()[STUDENT_ISSUES].find_one(
        {"_id": object_id, "student_id": student["_id"]}
    )
    if not issue:
        return None
    return serialize_mongo(_student_issue(issue))


async def reopen_student_issue(student: dict, issue_id: str) -> dict | None:
    try:
        object_id = to_object_id(issue_id)
    except ValueError as exc:
        raise ValueError("Invalid issue id") from exc
    db = get_database()
    issue = await db[STUDENT_ISSUES].find_one(
        {"_id": object_id, "student_id": student["_id"]}
    )
    if not issue:
        return None
    now = datetime.now(timezone.utc)
    await db[STUDENT_ISSUES].update_one(
        {"_id": object_id, "student_id": student["_id"], "status": {"$in": ["CLOSED", "RESOLVED"]}},
        {"$set": {
            "status": "IN_PROGRESS",
            "resolved_at": None,
            "updated_at": now,
            "updated_by_type": "STUDENT",
            "updated_by": {"id": student["_id"], "name": student.get("name"), "email": student.get("email")},
        }},
    )
    updated = await db[STUDENT_ISSUES].find_one({"_id": object_id, "student_id": student["_id"]})
    return serialize_mongo(_student_issue(updated))