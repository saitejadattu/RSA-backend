"""Backfill hiring_opportunities.shortlists_count from stored shortlist evidence.

Run without --apply to inspect planned updates. Pass --apply to write counts.
"""
import argparse
import asyncio

from app.config.settings import get_settings
from app.db.collections import APPLICATIONS, HIRING_OPPORTUNITIES
from motor.motor_asyncio import AsyncIOMotorClient


def shortlist_student_ids(applications: list[dict]) -> set:
    return {
        application["student_id"]
        for application in applications
        if application.get("student_id")
        and (
            (application.get("shortlist") or {}).get("is_shortlisted") is True
            or (application.get("screening") or {}).get("decision") == "shortlisted"
        )
    }


async def backfill(*, apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        db = client[settings.mongo_db_name]
        opportunities = await db[HIRING_OPPORTUNITIES].find(
            {"shortlist_imported_at": {"$exists": True}},
            {"_id": 1, "shortlists_count": 1},
        ).to_list(length=None)
        updated = 0
        for opportunity in opportunities:
            applications = await db[APPLICATIONS].find(
                {"opportunity_id": opportunity["_id"]},
                {"student_id": 1, "shortlist": 1, "screening": 1},
            ).to_list(length=None)
            count = len(shortlist_student_ids(applications))
            if opportunity.get("shortlists_count") == count:
                continue
            print(f"{opportunity['_id']}: {opportunity.get('shortlists_count')} -> {count}")
            if apply:
                await db[HIRING_OPPORTUNITIES].update_one(
                    {"_id": opportunity["_id"]},
                    {"$set": {"shortlists_count": count}},
                )
            updated += 1
        print(f"{'Updated' if apply else 'Would update'} {updated} opportunity(s).")
    finally:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the calculated counts.")
    args = parser.parse_args()
    asyncio.run(backfill(apply=args.apply))
