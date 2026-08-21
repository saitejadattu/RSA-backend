from fastapi import HTTPException, status

from app.db.collections import STUDENT_ISSUES, STUDENTS
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id


async def list_admin_issues(*, page: int = 1, limit: int = 50) -> dict:
    db = get_database()
    skip = (page - 1) * limit
    issues = await db[STUDENT_ISSUES].find({}).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)
    total = await db[STUDENT_ISSUES].count_documents({})
    open_count = await db[STUDENT_ISSUES].count_documents({"status": "OPEN"})
    resolved_count = await db[STUDENT_ISSUES].count_documents({"status": "RESOLVED"})

    student_ids = [issue["student_id"] for issue in issues if issue.get("student_id")]
    students = await db[STUDENTS].find(
        {"_id": {"$in": student_ids}}, {"name": 1, "email": 1}
    ).to_list(length=None) if student_ids else []
    student_by_id = {student["_id"]: student for student in students}

    rows = []
    for issue in issues:
        student = student_by_id.get(issue.get("student_id"), {})
        rows.append({
            **issue,
            "student": {
                "id": issue.get("student_id"),
                "name": student.get("name", "Student"),
                "email": student.get("email"),
            },
        })

    return serialize_mongo({
        "items": rows,
        "page": page,
        "limit": limit,
        "total": total,
        "summary": {
            "total": total,
            "open": open_count,
            "resolved": resolved_count,
        },
    })


async def get_admin_issue(issue_id: str) -> dict:
    db = get_database()
    try:
        object_id = to_object_id(issue_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid issue id") from exc

    issue = await db[STUDENT_ISSUES].find_one({"_id": object_id})
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    student = await db[STUDENTS].find_one(
        {"_id": issue.get("student_id")}, {"name": 1, "email": 1, "phone": 1}
    )
    return serialize_mongo({
        **issue,
        "student": {
            "id": issue.get("student_id"),
            "name": (student or {}).get("name", "Student"),
            "email": (student or {}).get("email"),
            "phone": (student or {}).get("phone"),
        },
    })
