"""Backfill responses_imported_at / shortlist_imported_at on openings that
already have data, so the skip-already-extracted guard works on today's data.

An opening counts as:
  - responses-extracted  if it has any application (someone applied/was imported)
  - shortlist-extracted   if it has any SHORTLISTED application or a shortlist sub-doc

Idempotent: only stamps openings that aren't stamped yet. --dry-run to preview.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS, HIRING_OPPORTUNITIES
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


async def main(dry_run: bool) -> None:
    await connect_to_mongo()
    db = get_database()
    now = datetime.now(timezone.utc)

    resp_stamped = short_stamped = 0
    async for opp in db[HIRING_OPPORTUNITIES].find(
        {}, {"responses_imported_at": 1, "shortlist_imported_at": 1}
    ):
        updates = {}
        if not opp.get("responses_imported_at"):
            if await db[APPLICATIONS].find_one({"opportunity_id": opp["_id"]}, {"_id": 1}):
                updates["responses_imported_at"] = now
        if not opp.get("shortlist_imported_at"):
            has_shortlist = await db[APPLICATIONS].find_one(
                {"opportunity_id": opp["_id"],
                 "$or": [{"current_status": "SHORTLISTED"}, {"shortlist": {"$ne": None}}]},
                {"_id": 1},
            )
            if has_shortlist:
                updates["shortlist_imported_at"] = now

        if updates and not dry_run:
            await db[HIRING_OPPORTUNITIES].update_one({"_id": opp["_id"]}, {"$set": updates})
        resp_stamped += 1 if "responses_imported_at" in updates else 0
        short_stamped += 1 if "shortlist_imported_at" in updates else 0

    print(f"mode: {'dry_run' if dry_run else 'apply'}")
    print(f"openings newly stamped responses_imported_at: {resp_stamped}")
    print(f"openings newly stamped shortlist_imported_at: {short_stamped}")
    await close_mongo_connection()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
