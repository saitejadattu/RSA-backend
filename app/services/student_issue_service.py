from datetime import datetime, timezone

from app.db.collections import STUDENT_ISSUES
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo


async def create_student_issue(student: dict, payload) -> dict:
    now = datetime.now(timezone.utc)
    document = {
        "student_id": student["_id"],
        "title": payload.title,
        "description": payload.description,
        "category": payload.category,
        "status": "OPEN",
        "created_at": now,
        "updated_at": now,
    }
    result = await get_database()[STUDENT_ISSUES].insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_mongo(document)