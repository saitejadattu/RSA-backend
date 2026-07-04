import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import (
    APPLICATIONS,
    COMPANIES,
    COMPANY_APPLICATIONS,
    COMPANY_SHORTLISTS,
    HIRING_OPPORTUNITIES,
)
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database

LEGACY_COMPANIES = "companies_legacy_stage1"
LEGACY_COMPANY_APPLICATIONS = "company_applications_legacy_stage1"
LEGACY_COMPANY_SHORTLISTS = "company_shortlists_legacy_stage1"


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def compact_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in value.items() if v is not None}


async def copy_collection_if_missing(db, source: str, target: str) -> int:
    if target in await db.list_collection_names():
        return await db[target].count_documents({})
    docs = await db[source].find({}).to_list(length=None)
    if docs:
        await db[target].insert_many(docs)
    return len(docs)


async def create_stage1_indexes(db) -> None:
    await db[COMPANIES].create_index([("company_key", ASCENDING)], unique=True)
    await db[COMPANIES].create_index([("name", ASCENDING)])

    await db[HIRING_OPPORTUNITIES].create_index([("company_id", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("role_key", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("opportunity_received_at", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index([("company_status", ASCENDING)])
    await db[HIRING_OPPORTUNITIES].create_index(
        [("company_id", ASCENDING), ("role_key", ASCENDING), ("opportunity_key", ASCENDING)],
        unique=True,
    )

    await db[APPLICATIONS].create_index([("student_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("company_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("opportunity_id", ASCENDING)])
    await db[APPLICATIONS].create_index([("status", ASCENDING)])
    await db[APPLICATIONS].create_index([("is_interested", ASCENDING)])
    await db[APPLICATIONS].create_index(
        [("opportunity_id", ASCENDING), ("student_id", ASCENDING)],
        unique=True,
    )


async def build_company_map(db, companies_source: str) -> tuple[dict[str, ObjectId], list[dict]]:
    legacy_companies = await db[companies_source].find({}).to_list(length=None)
    grouped: dict[str, dict] = {}
    for doc in legacy_companies:
        company_key = doc.get("company_key") or key(doc.get("company_name"))
        if not company_key:
            continue
        current = grouped.setdefault(
            company_key,
            {
                "_id": ObjectId(),
                "name": doc.get("company_name"),
                "company_key": company_key,
                "aliases": set(),
                "sources": set(),
                "created_at": doc.get("created_at") or datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        )
        if doc.get("company_name"):
            current["aliases"].add(doc["company_name"])
            if not current.get("name"):
                current["name"] = doc["company_name"]
        if doc.get("source"):
            current["sources"].add(doc["source"])

    companies: list[dict] = []
    company_map: dict[str, ObjectId] = {}
    for company_key, doc in grouped.items():
        doc["aliases"] = sorted(doc["aliases"])
        doc["sources"] = sorted(doc["sources"])
        companies.append(doc)
        company_map[company_key] = doc["_id"]
    return company_map, companies


def opportunity_from_legacy_company(doc: dict, normalized_company_id: ObjectId) -> dict:
    role = doc.get("role") or "unknown"
    role_key = doc.get("role_key") or key(role) or "unknown"
    opportunity_key = doc.get("opportunity_key") or key(
        doc.get("opportunity_received_at").isoformat()
        if doc.get("opportunity_received_at")
        else f"{role_key}-unknown-date"
    )
    return compact_dict(
        {
            "_id": ObjectId(),
            "legacy_company_id": doc["_id"],
            "company_id": normalized_company_id,
            "role": role,
            "role_key": role_key,
            "opportunity_key": opportunity_key,
            "opportunity_received_on": doc.get("opportunity_received_on"),
            "received_time": doc.get("received_time"),
            "opportunity_received_at": doc.get("opportunity_received_at"),
            "tech_stack": doc.get("tech_stack"),
            "must_have_skills": doc.get("must_have_skills"),
            "good_to_have_skills": doc.get("good_to_have_skills"),
            "positions": doc.get("positions"),
            "stipend": doc.get("stipend"),
            "location": doc.get("location"),
            "duration": doc.get("duration"),
            "day_timings": doc.get("day_timings"),
            "crm_poc": doc.get("crm_poc"),
            "student_side_status": doc.get("student_side_status"),
            "company_status": doc.get("company_status"),
            "student_response_sheet": doc.get("student_response_sheet"),
            "company_sheet": doc.get("company_sheet"),
            "hubspot_link": doc.get("hubspot_link"),
            "profiles_requested": doc.get("profiles_requested"),
            "profiles_shared": doc.get("profiles_shared"),
            "mapping_pool": doc.get("mapping_pool"),
            "eligible_as_per_pref": doc.get("eligible_as_per_pref"),
            "filled_form_count": doc.get("filled_form_count"),
            "interested_count": doc.get("interested_count"),
            "shortlists_count": doc.get("shortlists_count"),
            "process_datetime": doc.get("process_datetime"),
            "process_details": doc.get("process_details"),
            "screening_round": doc.get("screening_round"),
            "assignment_round": doc.get("assignment_round"),
            "tr_1": doc.get("tr_1"),
            "next_process": doc.get("next_process"),
            "company_feedback": doc.get("company_feedback"),
            "scheduled_date": doc.get("scheduled_date"),
            "interview_process": doc.get("interview_process"),
            "action_items": doc.get("action_items"),
            "hiring_intelligence": doc.get("hiring_intelligence"),
            "rsa_notes": doc.get("rsa_notes"),
            "source": doc.get("source"),
            "raw_company_row": doc.get("raw_company_row"),
            "created_at": doc.get("created_at") or datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
    )


async def build_opportunity_map(
    db, company_map: dict[str, ObjectId], companies_source: str
) -> tuple[dict[ObjectId, ObjectId], list[dict]]:
    legacy_companies = await db[companies_source].find({}).to_list(length=None)
    opportunities = []
    opportunity_map = {}
    seen = set()
    for doc in legacy_companies:
        company_key = doc.get("company_key") or key(doc.get("company_name"))
        normalized_company_id = company_map.get(company_key)
        if not normalized_company_id:
            continue
        opportunity = opportunity_from_legacy_company(doc, normalized_company_id)
        unique_key = (
            str(opportunity["company_id"]),
            opportunity.get("role_key"),
            opportunity.get("opportunity_key"),
        )
        if unique_key in seen:
            continue
        seen.add(unique_key)
        opportunities.append(opportunity)
        opportunity_map[doc["_id"]] = opportunity["_id"]
    return opportunity_map, opportunities


def normalized_status(app: dict, shortlist: dict | None) -> str:
    if shortlist:
        return shortlist.get("status") or "shortlisted"
    if app.get("is_interested") is False:
        return "not_interested"
    return app.get("status") or "applied"


async def build_applications(
    db,
    company_map: dict[str, ObjectId],
    opportunity_map: dict[ObjectId, ObjectId],
    companies_source: str,
    applications_source: str,
    shortlists_source: str,
) -> list[dict]:
    legacy_apps = await db[applications_source].find({}).to_list(length=None)
    shortlists = await db[shortlists_source].find({}).to_list(length=None)
    shortlist_by_application_id = {
        doc.get("application_id"): doc for doc in shortlists if doc.get("application_id")
    }

    applications = []
    seen = set()
    for app in legacy_apps:
        legacy_company = await db[companies_source].find_one({"_id": app["company_id"]})
        if not legacy_company:
            continue
        company_key = legacy_company.get("company_key") or key(legacy_company.get("company_name"))
        company_id = company_map.get(company_key)
        opportunity_id = opportunity_map.get(app["company_id"])
        student_id = app.get("student_id")
        if not company_id or not opportunity_id or not student_id:
            continue

        unique_key = (str(opportunity_id), str(student_id))
        if unique_key in seen:
            continue
        seen.add(unique_key)

        shortlist = shortlist_by_application_id.get(app["_id"])
        application_doc = compact_dict(
            {
                "_id": ObjectId(),
                "legacy_application_id": app["_id"],
                "student_id": student_id,
                "company_id": company_id,
                "opportunity_id": opportunity_id,
                "status": normalized_status(app, shortlist),
                "is_interested": app.get("is_interested"),
                "applied_at": app.get("applied_at"),
                "skills": app.get("skills") or {},
                "has_relevant_project_experience": app.get("has_relevant_project_experience"),
                "github_link": app.get("github_link"),
                "project_link": app.get("project_link"),
                "resume_link": app.get("resume_link"),
                "willing_remote": app.get("willing_remote"),
                "available_full_duration": app.get("available_full_duration"),
                "college_noc": app.get("college_noc"),
                "interest_reason": app.get("interest_reason"),
                "not_interested_reason": app.get("not_interested_reason"),
                "not_interested_other_reason": app.get("not_interested_other_reason"),
                "response_snapshot": compact_dict(
                    {
                        "student_uid": app.get("student_uid"),
                        "student_name": app.get("student_name"),
                        "email": app.get("email"),
                        "mobile": app.get("mobile"),
                        "company_name": app.get("company_name"),
                        "role": app.get("role"),
                    }
                ),
                "raw_response": app.get("raw_response"),
                "created_at": app.get("created_at") or datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        if shortlist:
            application_doc["shortlist"] = compact_dict(
                {
                    "is_shortlisted": True,
                    "shortlisted_at": app.get("shortlisted_at") or shortlist.get("updated_at"),
                    "resume": shortlist.get("resume"),
                    "resume_shortlisting": shortlist.get("resume_shortlisting"),
                    "final_status": shortlist.get("final_status"),
                    "raw_shortlist_row": shortlist.get("raw_shortlist_row"),
                    "source": "shortlist_sheet",
                }
            )
        applications.append(application_doc)
    return applications


async def migrate(apply: bool) -> dict:
    await connect_to_mongo()
    db = get_database()

    current_counts = {
        "companies": await db[COMPANIES].count_documents({}),
        "company_applications": await db[COMPANY_APPLICATIONS].count_documents({}),
        "company_shortlists": await db[COMPANY_SHORTLISTS].count_documents({}),
    }

    if not apply:
        company_map, companies = await build_company_map(db, COMPANIES)
        opportunity_map, opportunities = await build_opportunity_map(db, company_map, COMPANIES)
        applications = await build_applications(
            db,
            company_map,
            opportunity_map,
            COMPANIES,
            COMPANY_APPLICATIONS,
            COMPANY_SHORTLISTS,
        )
        await close_mongo_connection()
        return {
            "mode": "dry_run",
            "current_counts": current_counts,
            "would_create_unique_companies": len(companies),
            "would_create_hiring_opportunities": len(opportunities),
            "would_create_applications": len(applications),
            "note": "Run with --apply to backup legacy collections and write target Stage 1 collections.",
        }

    backup_counts = {
        LEGACY_COMPANIES: await copy_collection_if_missing(db, COMPANIES, LEGACY_COMPANIES),
        LEGACY_COMPANY_APPLICATIONS: await copy_collection_if_missing(
            db, COMPANY_APPLICATIONS, LEGACY_COMPANY_APPLICATIONS
        ),
        LEGACY_COMPANY_SHORTLISTS: await copy_collection_if_missing(
            db, COMPANY_SHORTLISTS, LEGACY_COMPANY_SHORTLISTS
        ),
    }

    await db[COMPANIES].drop()
    await db[HIRING_OPPORTUNITIES].drop()
    await db[APPLICATIONS].drop()

    company_map, companies = await build_company_map(db, LEGACY_COMPANIES)
    if companies:
        await db[COMPANIES].insert_many(companies)

    opportunity_map, opportunities = await build_opportunity_map(db, company_map, LEGACY_COMPANIES)
    if opportunities:
        await db[HIRING_OPPORTUNITIES].insert_many(opportunities)

    applications = await build_applications(
        db,
        company_map,
        opportunity_map,
        LEGACY_COMPANIES,
        LEGACY_COMPANY_APPLICATIONS,
        LEGACY_COMPANY_SHORTLISTS,
    )
    if applications:
        await db[APPLICATIONS].insert_many(applications)

    await create_stage1_indexes(db)
    final_counts = {
        "students": await db["students"].count_documents({}),
        "companies": await db[COMPANIES].count_documents({}),
        "hiring_opportunities": await db[HIRING_OPPORTUNITIES].count_documents({}),
        "applications": await db[APPLICATIONS].count_documents({}),
        "legacy_company_applications": await db[LEGACY_COMPANY_APPLICATIONS].count_documents({}),
        "legacy_company_shortlists": await db[LEGACY_COMPANY_SHORTLISTS].count_documents({}),
    }
    await close_mongo_connection()
    return {"mode": "apply", "current_counts": current_counts, "backup_counts": backup_counts, "final_counts": final_counts}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate current company/application schema to Stage 1 schema.")
    parser.add_argument("--apply", action="store_true", help="Write migration changes. Without this, only prints a dry run.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    import json

    print(json.dumps(asyncio.run(migrate(apply=args.apply)), default=str, indent=2))
