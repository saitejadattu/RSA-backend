import asyncio
import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS, COMPANIES, HIRING_OPPORTUNITIES, STUDENTS
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database

SKILLS = ("python", "nodejs", "react", "mongodb", "sql", "dsa", "javascript")


async def main() -> None:
    await connect_to_mongo()
    db = get_database()
    out: dict = {}

    out["totals"] = {
        "students": await db[STUDENTS].count_documents({}),
        "companies": await db[COMPANIES].count_documents({}),
        "opportunities": await db[HIRING_OPPORTUNITIES].count_documents({}),
        "applications": await db[APPLICATIONS].count_documents({}),
    }

    # Status distribution
    out["status"] = await db[APPLICATIONS].aggregate([
        {"$group": {"_id": "$current_status", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]).to_list(length=None)

    # Applications over time by month (applied_at)
    out["by_month"] = await db[APPLICATIONS].aggregate([
        {"$match": {"applied_at": {"$ne": None}}},
        {"$group": {"_id": {"$dateToString": {"format": "%Y-%m", "date": "$applied_at"}}, "n": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ]).to_list(length=None)

    # Top companies by application count
    out["top_companies"] = await db[APPLICATIONS].aggregate([
        {"$group": {"_id": "$company_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
        {"$limit": 12},
        {"$lookup": {"from": COMPANIES, "localField": "_id", "foreignField": "_id", "as": "c"}},
        {"$unwind": {"path": "$c", "preserveNullAndEmptyArrays": True}},
        {"$project": {"_id": 0, "name": "$c.name", "n": 1}},
    ]).to_list(length=None)

    # Average self-assessment per skill (only where a score exists)
    skill_avgs = {}
    for s in SKILLS:
        field = f"application_details.self_assessment.{s}"
        res = await db[APPLICATIONS].aggregate([
            {"$match": {field: {"$type": "number"}}},
            {"$group": {"_id": None, "avg": {"$avg": f"${field}"}, "n": {"$sum": 1}}},
        ]).to_list(length=1)
        skill_avgs[s] = res[0] if res else {"avg": None, "n": 0}
    out["skill_avgs"] = skill_avgs

    # Interested vs not
    out["interested"] = {
        "interested": await db[APPLICATIONS].count_documents({"application_details.interested": True}),
        "not_interested": await db[APPLICATIONS].count_documents({"application_details.interested": False}),
    }

    # Placement outcomes
    out["offer_status"] = await db[APPLICATIONS].aggregate([
        {"$match": {"placement.offer_letter.status": {"$ne": None}}},
        {"$group": {"_id": "$placement.offer_letter.status", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]).to_list(length=None)
    out["internship_status"] = await db[APPLICATIONS].aggregate([
        {"$match": {"placement.internship.status": {"$ne": None}}},
        {"$group": {"_id": "$placement.internship.status", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]).to_list(length=None)
    out["selected_total"] = await db[APPLICATIONS].count_documents({"placement.selected": True})

    print(json.dumps(out, default=str, indent=2))
    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(main())
