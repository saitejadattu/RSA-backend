from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.db.collections import APPLICATIONS, STATUS_HISTORY
from app.db.mongodb import get_database
from app.schemas.status_history import ApplicationStatusUpdate
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


async def add_status_history(
    *,
    application: dict,
    old_status: str | None,
    new_status: str,
    reason: str | None = None,
    notes: str | None = None,
    changed_by: str | None = None,
    changed_by_role: str = "admin",
    source: str = "manual",
) -> dict:
    db = get_database()
    now = datetime.now(timezone.utc)
    document = {
        "application_id": application["_id"],
        "student_id": application.get("student_id"),
        "company_id": application.get("company_id"),
        "opportunity_id": application.get("opportunity_id"),
        "old_status": old_status,
        "new_status": new_status,
        "reason": reason,
        "notes": notes,
        "changed_by": to_object_id(changed_by) if changed_by else None,
        "changed_by_role": changed_by_role,
        "source": source,
        "created_at": now,
    }
    result = await db[STATUS_HISTORY].insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_mongo(document)


async def update_application_status(application_id: str, payload: ApplicationStatusUpdate) -> dict:
    db = get_database()
    try:
        object_id = to_object_id(application_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid application id")

    application = await db[APPLICATIONS].find_one({"_id": object_id})
    if not application:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")

    old_status = application.get("status")
    now = datetime.now(timezone.utc)
    update_fields = {
        "status": payload.new_status,
        "updated_at": now,
    }
    if payload.new_status == "shortlisted":
        update_fields["shortlisted_at"] = now
    elif payload.new_status == "rejected":
        update_fields["rejected_at"] = now
    elif payload.new_status == "hired":
        update_fields["hired_at"] = now

    await db[APPLICATIONS].update_one({"_id": object_id}, {"$set": update_fields})
    history = await add_status_history(
        application=application,
        old_status=old_status,
        new_status=payload.new_status,
        reason=payload.reason,
        notes=payload.notes,
        changed_by=payload.changed_by,
        changed_by_role=payload.changed_by_role,
        source=payload.source,
    )
    updated_application = await db[APPLICATIONS].find_one({"_id": object_id})
    return {"application": serialize_mongo(updated_application), "status_history": history}


async def list_status_history(application_id: str) -> list[dict]:
    db = get_database()
    try:
        object_id = to_object_id(application_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid application id")

    history = await db[STATUS_HISTORY].find({"application_id": object_id}).sort("created_at", 1).to_list(length=None)
    return serialize_mongo(history)
