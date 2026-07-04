import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS, STATUS_HISTORY
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database


async def backfill(apply: bool) -> dict:
    await connect_to_mongo()
    await create_indexes()
    db = get_database()

    applications = await db[APPLICATIONS].find({}).to_list(length=None)
    inserted = skipped_existing = 0
    now = datetime.now(timezone.utc)

    for application in applications:
        exists = await db[STATUS_HISTORY].find_one(
            {
                "application_id": application["_id"],
                "source": "stage2_backfill",
            },
            {"_id": 1},
        )
        if exists:
            skipped_existing += 1
            continue

        if apply:
            await db[STATUS_HISTORY].insert_one(
                {
                    "application_id": application["_id"],
                    "student_id": application.get("student_id"),
                    "company_id": application.get("company_id"),
                    "opportunity_id": application.get("opportunity_id"),
                    "old_status": None,
                    "new_status": application.get("status"),
                    "reason": "Initial Stage 2 status history backfill",
                    "notes": None,
                    "changed_by": None,
                    "changed_by_role": "system",
                    "source": "stage2_backfill",
                    "created_at": application.get("created_at") or now,
                }
            )
        inserted += 1

    total_history = await db[STATUS_HISTORY].count_documents({})
    await close_mongo_connection()
    return {
        "mode": "apply" if apply else "dry_run",
        "applications_checked": len(applications),
        "would_insert_or_inserted": inserted,
        "skipped_existing": skipped_existing,
        "status_history_count": total_history,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill status_history from existing applications.")
    parser.add_argument("--apply", action="store_true", help="Write backfill records. Without this, only dry-runs.")
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(backfill(parse_args().apply)))
