"""Backfill derived application and shortlist counters on every opportunity.

Run with --dry-run first to inspect the calculated values without writing to MongoDB.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(BACKEND_ROOT))
os.chdir(BACKEND_ROOT)

from app.db.collections import APPLICATIONS, HIRING_OPPORTUNITIES
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


COUNTER_FIELDS = {"application_count", "shortlists_count"}


def _status_value(application: dict[str, Any]) -> Any:
    return application.get("current_status") or application.get("status")


def counts_for_application(application: dict[str, Any]) -> tuple[bool, bool]:
    """Return whether one application contributes to Applied and Shortlisted."""
    details = application.get("application_details")
    if isinstance(details, dict) and "interested" in details:
        is_real_application = details.get("interested") is not False
    else:
        is_real_application = application.get("is_interested") is not False

    status = _status_value(application)
    if str(status).lower() == "not_interested":
        is_real_application = False

    is_shortlisted = False
    if is_real_application:
        is_shortlisted = (
            (application.get("shortlist") or {}).get("is_shortlisted") is True
            or (application.get("screening") or {}).get("decision") == "shortlisted"
            or str(status) in {"SHORTLISTED", "shortlisted"}
        )
    return is_real_application, is_shortlisted


def calculate_counts(applications: list[dict[str, Any]]) -> tuple[int, int]:
    applied = shortlisted = 0
    for application in applications:
        is_applied, is_shortlisted = counts_for_application(application)
        applied += int(is_applied)
        shortlisted += int(is_shortlisted)
    return applied, shortlisted


@dataclass
class Summary:
    processed: int = 0
    updated: int = 0
    unchanged: int = 0
    total_applied: int = 0
    total_shortlisted: int = 0
    response_yes: int = 0
    response_no: int = 0
    shortlist_yes: int = 0
    shortlist_no: int = 0
    errors: int = 0


async def backfill(*, dry_run: bool, db: Any | None = None) -> Summary:
    owns_connection = db is None
    if owns_connection:
        await connect_to_mongo()
        db = get_database()

    summary = Summary()
    try:
        collections = await db.list_collection_names()
        if HIRING_OPPORTUNITIES not in collections:
            raise RuntimeError(f"MongoDB collection '{HIRING_OPPORTUNITIES}' does not exist")

        opportunities = await db[HIRING_OPPORTUNITIES].find({}).to_list(length=None)
        print(f"Opportunities to process: {len(opportunities)}")

        applications_by_opportunity: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        applications = await db[APPLICATIONS].find(
            {}, {"opportunity_id": 1, "application_details": 1, "is_interested": 1,
                "current_status": 1, "status": 1, "shortlist": 1, "screening": 1}
        ).to_list(length=None)
        for application in applications:
            applications_by_opportunity[application.get("opportunity_id")].append(application)

        for opportunity in opportunities:
            opportunity_id = opportunity.get("_id")
            company = opportunity.get("company_name") or opportunity.get("company") or "-"
            role = opportunity.get("role") or "-"
            try:
                related = applications_by_opportunity.get(opportunity_id, [])
                applied, shortlisted = calculate_counts(related)
                response_exists = bool(str(opportunity.get("student_response_sheet") or "").strip())
                shortlist_exists = bool(str(opportunity.get("company_sheet") or "").strip())
                summary.processed += 1
                summary.total_applied += applied
                summary.total_shortlisted += shortlisted
                summary.response_yes += int(response_exists)
                summary.response_no += int(not response_exists)
                summary.shortlist_yes += int(shortlist_exists)
                summary.shortlist_no += int(not shortlist_exists)

                current_applied = opportunity.get("application_count", 0)
                current_shortlisted = opportunity.get("shortlists_count", 0)
                changed = current_applied != applied or current_shortlisted != shortlisted
                action = "WOULD UPDATE" if dry_run and changed else "UPDATE" if changed else "NO CHANGE"
                print(
                    f"Opportunity: {role}\n"
                    f"ID: {opportunity_id}\n"
                    f"Company: {company}\n"
                    f"Response sheet: {'YES' if response_exists else 'NO'}\n"
                    f"Shortlist sheet: {'YES' if shortlist_exists else 'NO'}\n"
                    f"Current Applied: {current_applied}\n"
                    f"Calculated Applied: {applied}\n"
                    f"Current Shortlisted: {current_shortlisted}\n"
                    f"Calculated Shortlisted: {shortlisted}\n"
                    f"Action: {action}"
                )
                if not changed:
                    summary.unchanged += 1
                    continue
                if dry_run:
                    summary.updated += 1
                    continue

                await db[HIRING_OPPORTUNITIES].update_one(
                    {"_id": opportunity_id},
                    {"$set": {"application_count": int(applied), "shortlists_count": int(shortlisted)}},
                )
                summary.updated += 1
            except Exception as exc:
                summary.errors += 1
                print(f"Opportunity ID: {opportunity_id}\nCompany: {company}\nRole: {role}\nError: {exc}")

        print_summary(summary, dry_run=dry_run)
        return summary
    finally:
        if owns_connection:
            await close_mongo_connection()


def print_summary(summary: Summary, *, dry_run: bool) -> None:
    print(
        f"\nTotal opportunities processed: {summary.processed}\n"
        f"Opportunities {'would be updated' if dry_run else 'updated'}: {summary.updated}\n"
        f"Opportunities unchanged: {summary.unchanged}\n\n"
        f"Total Applied counts calculated: {summary.total_applied}\n"
        f"Total Shortlisted counts calculated: {summary.total_shortlisted}\n\n"
        f"Opportunities with response sheet: {summary.response_yes}\n"
        f"Opportunities without response sheet: {summary.response_no}\n\n"
        f"Opportunities with shortlist sheet: {summary.shortlist_yes}\n"
        f"Opportunities without shortlist sheet: {summary.shortlist_no}\n\n"
        f"Errors: {summary.errors}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report changes without modifying MongoDB")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))
