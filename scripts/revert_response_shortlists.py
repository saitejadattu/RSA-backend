"""Revert candidates who were marked SHORTLISTED only by a response-sheet remark.

The response sheet's "Shortlisted" remark is not authoritative - the shortlist
(company) sheet is. Some applications were promoted to SHORTLISTED purely from a
response remark and were never confirmed by a shortlist import. Put those back to
APPLIED and drop the misleading "Shortlisted" feedback.

A candidate that a shortlist import DID confirm (shortlisted_at / is_shortlisted
set) is left untouched, even if a response remark also mentioned shortlisting.

--dry-run to preview counts without writing.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.application import final_status_for, normalize_application_status

# SHORTLISTED, promoted by a response remark, and NOT confirmed by any shortlist
# import (no shortlisted_at, not is_shortlisted).
QUERY = {
    "current_status": "SHORTLISTED",
    "screening.source": "response_sheet",
    "screening.decision": "shortlisted",
    "is_shortlisted": {"$ne": True},
    "$or": [{"shortlisted_at": None}, {"shortlisted_at": {"$exists": False}}],
}


async def main(dry_run: bool) -> None:
    await connect_to_mongo()
    db = get_database()
    now = datetime.now(timezone.utc)

    reverted = 0
    async for app in db[APPLICATIONS].find(QUERY):
        interested = ((app.get("application_details") or {}).get("interested"))
        interested = True if interested is None else bool(interested)
        new_status = normalize_application_status(None, interested=interested)  # APPLIED
        student = await db["students"].find_one({"_id": app["student_id"]}, {"name": 1})
        reverted += 1
        print(f"  {(student or {}).get('name', '?')[:26]:28s} SHORTLISTED -> {new_status}")
        if not dry_run:
            await db[APPLICATIONS].update_one(
                {"_id": app["_id"]},
                {
                    "$set": {
                        "current_status": new_status,
                        "final_status": final_status_for(new_status, interested=interested),
                        "updated_at": now,
                    },
                    # The only remark was the (non-authoritative) shortlist marker.
                    "$unset": {"screening": ""},
                },
            )

    print(f"\nmode: {'dry_run' if dry_run else 'apply'}")
    print(f"reverted to APPLIED: {reverted}")
    await close_mongo_connection()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
