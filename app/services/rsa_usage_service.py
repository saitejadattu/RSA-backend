from datetime import datetime, timezone
from bson import ObjectId

from app.db.collections import RSA_USAGE, STUDENTS
from app.db.mongodb import get_database
from app.utils.mongo import serialize_mongo


async def record_rsa_hit(student_id: ObjectId) -> dict:
    """Record an RSA usage hit for a student."""
    db = get_database()
    
    result = await db[RSA_USAGE].insert_one({
        "student_id": student_id,
        "opened_at": datetime.now(timezone.utc),
    })
    
    return {"success": True}


async def get_admin_rsa_usage() -> dict:
    """Get RSA usage statistics for admin dashboard."""
    db = get_database()
    
    # Get total number of students
    total_students = await db[STUDENTS].count_documents({})
    
    # Get total number of rsa_usage records
    total_hits = await db[RSA_USAGE].count_documents({})
    
    # Get unique students who have opened RSA
    unique_students_result = await db[RSA_USAGE].aggregate([
        {"$group": {"_id": "$student_id"}},
        {"$count": "count"}
    ]).to_list(None)
    
    unique_students = unique_students_result[0]["count"] if unique_students_result else 0
    
    # Calculate not_opened (students who never opened RSA)
    not_opened = max(0, total_students - unique_students)
    
    # Get detailed student data with hit counts and last opened time
    students_with_hits = await db[RSA_USAGE].aggregate([
        {
            "$group": {
                "_id": "$student_id",
                "hit_count": {"$sum": 1},
                "last_opened_at": {"$max": "$opened_at"}
            }
        },
        {"$sort": {"last_opened_at": -1}},
        {
            "$lookup": {
                "from": STUDENTS,
                "localField": "_id",
                "foreignField": "_id",
                "as": "student_doc"
            }
        },
        {"$unwind": "$student_doc"},
        {
            "$project": {
                "student_id": "$_id",
                "name": "$student_doc.name",
                "hit_count": 1,
                "last_opened_at": 1,
                "_id": 0
            }
        },
        {"$sort": {"last_opened_at": -1}}
    ]).to_list(None)
    
    # Serialize the response
    return serialize_mongo({
        "total_students": total_students,
        "unique_students": unique_students,
        "total_hits": total_hits,
        "not_opened": not_opened,
        "students": students_with_hits
    })
