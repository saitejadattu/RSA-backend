"""Reset a wrongly-extracted interview session so it can be re-extracted cleanly.

An extraction with a bad speaker map leaves three kinds of debris that a plain
re-run does NOT clean up:
  * per-student reports written under the WRONG student (orphaned on re-run),
  * the questions persisted for the session,
  * application status changes (SHORTLISTED -> INTERVIEW_COMPLETED / NOT_ATTENDED).

This script undoes all three for the targeted session(s), reverting each
application to the status it held *before* the interview analysis (from
status_history, falling back to SHORTLISTED), and resets the session so the
normal "extract transcript" flow can be run again from scratch. The transcript
itself is kept.

Select the session by ONE of --session / --opportunity / --company.
Dry-run by default; pass --apply to actually write.

    python scripts/reset_extraction.py --company "BizPlus4u"
    python scripts/reset_extraction.py --company "BizPlus4u" --apply
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app.db.collections import (  # noqa: E402
    APPLICATIONS,
    COMPANIES,
    HIRING_OPPORTUNITIES,
    INTERVIEW_REPORTS,
    INTERVIEW_SESSIONS,
    QUESTIONS,
    STATUS_HISTORY,
    STUDENTS,
)

# Statuses that only an interview extraction sets — the ones we revert.
INTERVIEW_DRIVEN = {"INTERVIEW_COMPLETED", "INTERVIEW_NOT_ATTENDED", "INTERVIEW_IN_PROGRESS"}


async def resolve_sessions(db, args) -> list[dict]:
    if args.session:
        session = await db[INTERVIEW_SESSIONS].find_one({"_id": ObjectId(args.session)})
        return [session] if session else []
    if args.opportunity:
        return await db[INTERVIEW_SESSIONS].find({"opportunity_id": ObjectId(args.opportunity)}).to_list(None)
    # by company name (case-insensitive substring)
    companies = await db[COMPANIES].find(
        {"name": {"$regex": args.company, "$options": "i"}}, {"_id": 1, "name": 1}
    ).to_list(None)
    if not companies:
        return []
    opps = await db[HIRING_OPPORTUNITIES].find(
        {"company_id": {"$in": [c["_id"] for c in companies]}}, {"_id": 1}
    ).to_list(None)
    if not opps:
        return []
    return await db[INTERVIEW_SESSIONS].find(
        {"opportunity_id": {"$in": [o["_id"] for o in opps]}}
    ).to_list(None)


async def status_before_interview(db, application_id) -> str:
    """The status this application held before its first interview-analysis change."""
    entry = await db[STATUS_HISTORY].find_one(
        {"application_id": application_id, "source": "interview_analysis"},
        sort=[("created_at", 1)],
    )
    prior = (entry or {}).get("old_status")
    return prior if prior and prior not in INTERVIEW_DRIVEN else "SHORTLISTED"


async def process_session(db, session, *, apply: bool, now) -> None:
    session_id = session["_id"]
    opp_id = session.get("opportunity_id")
    opp = await db[HIRING_OPPORTUNITIES].find_one({"_id": opp_id}, {"company_name": 1, "role": 1}) or {}
    label = f"{opp.get('company_name', '?')} - {opp.get('role', '?')}"

    n_reports = await db[INTERVIEW_REPORTS].count_documents({"session_id": session_id})
    n_questions = await db[QUESTIONS].count_documents({"session_id": session_id})
    apps = await db[APPLICATIONS].find(
        {"opportunity_id": opp_id, "current_status": {"$in": list(INTERVIEW_DRIVEN)}},
        {"_id": 1, "student_id": 1, "current_status": 1},
    ).to_list(None)

    print(f"\n== Session {session_id}  [{label}]")
    print(f"    reports to delete   : {n_reports}")
    print(f"    questions to delete : {n_questions}")
    print(f"    applications to revert: {len(apps)}")
    for app in apps:
        student = await db[STUDENTS].find_one({"_id": app.get("student_id")}, {"name": 1})
        restore = await status_before_interview(db, app["_id"])
        print(f"      - {(student or {}).get('name', '?'):28} {app['current_status']} -> {restore}")

    if not apply:
        return

    for app in apps:
        restore = await status_before_interview(db, app["_id"])
        await db[APPLICATIONS].update_one(
            {"_id": app["_id"]},
            {"$set": {"current_status": restore, "updated_at": now}},
        )
    # Drop the interview-analysis audit entries so history matches the revert.
    app_ids = [app["_id"] for app in apps]
    if app_ids:
        await db[STATUS_HISTORY].delete_many(
            {"application_id": {"$in": app_ids}, "source": "interview_analysis"}
        )
    await db[INTERVIEW_REPORTS].delete_many({"session_id": session_id})
    await db[QUESTIONS].delete_many({"session_id": session_id})
    await db[INTERVIEW_SESSIONS].update_one(
        {"_id": session_id},
        {
            "$set": {"ai_status": "pending", "processed": False, "updated_at": now},
            "$unset": {"company_expectations": "", "speaker_map": ""},
        },
    )
    print("    [done] reset")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", help="Interview session _id")
    group.add_argument("--opportunity", help="Hiring opportunity _id")
    group.add_argument("--company", help="Company name (case-insensitive substring)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry-run)")
    args = parser.parse_args()

    load_dotenv(os.path.join(_ROOT, ".env"))
    db = AsyncIOMotorClient(os.environ["MONGO_URI"])[os.environ["MONGO_DB_NAME"]]
    now = datetime.now(timezone.utc)

    sessions = await resolve_sessions(db, args)
    if not sessions:
        print("No matching interview sessions found.")
        return

    print(f"{'APPLY' if args.apply else 'DRY-RUN'} - {len(sessions)} session(s) matched")
    for session in sessions:
        await process_session(db, session, apply=args.apply, now=now)

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to perform the reset.")
    else:
        print("\nDone. Now re-extract the transcript from the opportunity's page.")


if __name__ == "__main__":
    asyncio.run(main())
