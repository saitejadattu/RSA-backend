import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS, STUDENTS
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


def normalize_company_name(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", " ", value.strip().lower())


async def duplicate_students_by_field(db, field: str) -> list[dict]:
    return await db[STUDENTS].aggregate(
        [
            {"$match": {field: {"$type": "string", "$ne": ""}}},
            {
                "$group": {
                    "_id": f"${field}",
                    "count": {"$sum": 1},
                    "student_ids": {"$push": "$_id"},
                    "names": {"$addToSet": "$name"},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ]
    ).to_list(length=None)


async def duplicate_applications(db) -> list[dict]:
    return await db[COMPANY_APPLICATIONS].aggregate(
        [
            {
                "$group": {
                    "_id": {
                        "student_id": "$student_id",
                        "company_id": "$company_id",
                        "role": "$role",
                    },
                    "count": {"$sum": 1},
                    "application_ids": {"$push": "$_id"},
                    "student_names": {"$addToSet": "$student_name"},
                    "company_names": {"$addToSet": "$company_name"},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)


async def normalized_company_name_collisions(db) -> list[dict]:
    companies = await db[COMPANIES].find(
        {},
        {
            "company_name": 1,
            "company_key": 1,
            "role": 1,
            "opportunity_received_at": 1,
            "source": 1,
        },
    ).to_list(length=None)

    grouped: dict[str, dict] = {}
    for company in companies:
        normalized = normalize_company_name(company.get("company_name"))
        if not normalized:
            continue
        group = grouped.setdefault(
            normalized,
            {
                "normalized_name": normalized,
                "count": 0,
                "company_ids": [],
                "company_names": set(),
                "company_keys": set(),
                "roles": set(),
                "sources": set(),
            },
        )
        group["count"] += 1
        group["company_ids"].append(company["_id"])
        if company.get("company_name"):
            group["company_names"].add(company["company_name"])
        if company.get("company_key"):
            group["company_keys"].add(company["company_key"])
        if company.get("role"):
            group["roles"].add(company["role"])
        if company.get("source"):
            group["sources"].add(company["source"])

    collisions = []
    for group in grouped.values():
        if group["count"] > 1:
            collisions.append(
                {
                    **group,
                    "company_names": sorted(group["company_names"]),
                    "company_keys": sorted(group["company_keys"]),
                    "roles": sorted(group["roles"]),
                    "sources": sorted(group["sources"]),
                }
            )
    return sorted(collisions, key=lambda item: (-item["count"], item["normalized_name"]))


async def duplicate_opportunities(db) -> list[dict]:
    return await db[COMPANIES].aggregate(
        [
            {
                "$group": {
                    "_id": {
                        "company_key": "$company_key",
                        "role_key": "$role_key",
                        "opportunity_received_at": "$opportunity_received_at",
                    },
                    "count": {"$sum": 1},
                    "company_ids": {"$push": "$_id"},
                    "company_names": {"$addToSet": "$company_name"},
                    "roles": {"$addToSet": "$role"},
                    "sources": {"$addToSet": "$source"},
                }
            },
            {"$match": {"count": {"$gt": 1}}},
            {"$sort": {"count": -1}},
        ]
    ).to_list(length=None)


async def unmatched_shortlists(db) -> list[dict]:
    return await db[COMPANY_SHORTLISTS].find(
        {"$or": [{"matched_application": False}, {"application_id": None}]},
        {
            "company_name": 1,
            "role": 1,
            "full_name": 1,
            "email": 1,
            "matched_application": 1,
            "application_id": 1,
        },
    ).to_list(length=None)


async def run_validation() -> dict:
    await connect_to_mongo()
    db = get_database()

    duplicate_emails = await duplicate_students_by_field(db, "email")
    duplicate_phones = await duplicate_students_by_field(db, "phone")
    duplicate_apps = await duplicate_applications(db)
    company_name_collisions = await normalized_company_name_collisions(db)
    duplicate_opps = await duplicate_opportunities(db)
    unmatched = await unmatched_shortlists(db)

    report = {
        "checks": {
            "duplicate_students_by_email": {
                "passed": len(duplicate_emails) == 0,
                "count": len(duplicate_emails),
                "items": duplicate_emails,
            },
            "duplicate_students_by_phone": {
                "passed": len(duplicate_phones) == 0,
                "count": len(duplicate_phones),
                "items": duplicate_phones,
            },
            "duplicate_applications_by_student_company_role": {
                "passed": len(duplicate_apps) == 0,
                "count": len(duplicate_apps),
                "items": duplicate_apps,
            },
            "normalized_company_name_collisions": {
                "passed": True,
                "count": len(company_name_collisions),
                "items": company_name_collisions,
                "note": "Collisions are expected when one company has multiple opportunities; review names for spelling variants.",
            },
            "duplicate_opportunities_by_company_role_received_at": {
                "passed": len(duplicate_opps) == 0,
                "count": len(duplicate_opps),
                "items": duplicate_opps,
            },
            "unmatched_shortlist_records": {
                "passed": len(unmatched) == 0,
                "count": len(unmatched),
                "items": unmatched,
            },
        }
    }
    report["ready_for_stage1_migration"] = all(
        check["passed"]
        for name, check in report["checks"].items()
        if name != "normalized_company_name_collisions"
    )

    await close_mongo_connection()
    return report


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_validation()), default=str, indent=2))
