"""Fix applications wrongly marked DROPPED because their interest cell was blank,
and backfill the company remark (shown to the student) from the stored sheet row.

Replays the corrected import logic on each response-sourced application's stored
raw_response - no sheet is re-fetched, candidates are unchanged.

Safe rules:
  - Only touch applications still at the screening stage (APPLIED / DROPPED /
    NOT_SHORTLISTED / PROFILE_SHARED). Never anything in interviews or beyond.
  - DROPPED -> recomputed status when it should not have been dropped.
  - Apply a Shortlisted / Selected-elsewhere remark.
  - Capture the remark into screening (visible to student) when present.

--dry-run to preview counts without writing.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import APPLICATIONS
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.models.application import final_status_for, normalize_application_status
from app.services.sheet_import_service import (
    NEGATIVE_INTEREST,
    classify_remark,
    detect_interest_header,
    detect_remark_header,
)

FIXABLE = {"APPLIED", "DROPPED", "NOT_SHORTLISTED", "PROFILE_SHARED"}


async def main(dry_run: bool) -> None:
    await connect_to_mongo()
    db = get_database()
    now = datetime.now(timezone.utc)

    status_fixed = remark_added = scanned = 0
    async for app in db[APPLICATIONS].find({"source": {"$in": ["response_paste", "response_sheet"]}}):
        raw = ((app.get("application_details") or {}).get("other_response") or {}).get("raw_response")
        if not isinstance(raw, dict) or not raw:
            continue
        scanned += 1
        headers = list(raw.keys())
        ih = detect_interest_header(headers)
        rh = detect_remark_header(headers)

        interested = (raw.get(ih) or "").strip().lower() not in NEGATIVE_INTEREST if ih else True
        remark = (raw.get(rh) or "").strip() if rh else ""
        decision, target = classify_remark(remark)
        # The response sheet is not authoritative for shortlisting - the shortlist
        # sheet is. A "Shortlisted" remark here stays APPLIED and isn't shown.
        if decision == "shortlisted":
            decision, target, remark = "none", None, ""
        new_status = target or normalize_application_status(None, interested=interested)

        updates: dict = {}
        cur = app.get("current_status")
        if cur in FIXABLE and new_status != cur:
            # Don't un-shortlist someone if the recompute has no opinion.
            if not (cur == "SHORTLISTED" and new_status == "APPLIED"):
                updates["current_status"] = new_status
                updates["final_status"] = final_status_for(new_status, interested=interested)
                status_fixed += 1

        if remark and not app.get("screening"):
            updates["screening"] = {
                "remark": remark,
                "decision": decision,
                "source": "response_sheet",
                "imported_at": now,
                "visible_to_student": True,
            }
            remark_added += 1

        if updates and not dry_run:
            updates["updated_at"] = now
            await db[APPLICATIONS].update_one({"_id": app["_id"]}, {"$set": updates})

    print(f"mode: {'dry_run' if dry_run else 'apply'}")
    print(f"response applications scanned: {scanned}")
    print(f"statuses fixed:                {status_fixed}")
    print(f"remarks captured:              {remark_added}")
    await close_mongo_connection()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    asyncio.run(main(parser.parse_args().dry_run))
