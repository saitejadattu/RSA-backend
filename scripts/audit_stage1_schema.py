import asyncio
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS, STUDENTS
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


async def run_audit() -> dict:
    await connect_to_mongo()
    db = get_database()

    collections = await db.list_collection_names()
    counts = {
        name: await db[name].count_documents({})
        for name in [STUDENTS, COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS]
        if name in collections
    }

    duplicate_company_records = await db[COMPANIES].aggregate(
        [
            {
                "$group": {
                    "_id": "$company_key",
                    "count": {"$sum": 1},
                    "company_names": {"$addToSet": "$company_name"},
                    "roles": {"$addToSet": "$role"},
                    "sources": {"$addToSet": "$source"},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    ).to_list(length=None)

    response_company_records = await db[COMPANIES].aggregate(
        [
            {"$match": {"source": "response_sheet_import"}},
            {
                "$lookup": {
                    "from": COMPANY_APPLICATIONS,
                    "localField": "_id",
                    "foreignField": "company_id",
                    "as": "applications",
                }
            },
            {
                "$project": {
                    "company_name": 1,
                    "role": 1,
                    "company_key": 1,
                    "role_key": 1,
                    "application_count": {"$size": "$applications"},
                }
            },
            {"$sort": {"company_name": 1, "role": 1}},
        ]
    ).to_list(length=None)

    application_groups = await db[COMPANY_APPLICATIONS].aggregate(
        [
            {
                "$group": {
                    "_id": {"company_name": "$company_name", "role": "$role"},
                    "count": {"$sum": 1},
                    "interested": {"$sum": {"$cond": ["$is_interested", 1, 0]}},
                    "shortlisted": {
                        "$sum": {"$cond": [{"$eq": ["$status", "shortlisted"]}, 1, 0]}
                    },
                    "company_ids": {"$addToSet": "$company_id"},
                }
            },
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)

    shortlist_groups = await db[COMPANY_SHORTLISTS].aggregate(
        [
            {
                "$group": {
                    "_id": {"company_name": "$company_name", "role": "$role"},
                    "count": {"$sum": 1},
                    "matched": {"$sum": {"$cond": ["$matched_application", 1, 0]}},
                    "unmatched": {"$sum": {"$cond": ["$matched_application", 0, 1]}},
                }
            },
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)

    students_with_applications = [
        value for value in await db[COMPANY_APPLICATIONS].distinct("student_id") if value
    ]
    student_application_summary = {
        "students_total": await db[STUDENTS].count_documents({}),
        "students_with_applications": len(students_with_applications),
        "students_without_applications": await db[STUDENTS].count_documents(
            {"_id": {"$nin": students_with_applications}}
        ),
    }

    await close_mongo_connection()
    return {
        "counts": counts,
        "duplicate_company_records": duplicate_company_records,
        "response_company_records": response_company_records,
        "application_groups": application_groups,
        "shortlist_groups": shortlist_groups,
        "student_application_summary": student_application_summary,
        "stage1_findings": [
            "companies currently mixes company identity and opportunity data",
            "company_applications duplicates company_name, role, student_name, email, and mobile",
            "company_shortlists should be merged into application status/shortlist metadata",
            "response-sheet company records can become orphaned if an import fails after company creation",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_audit()), default=str, indent=2))
