"""Backfill transcript_hash on existing transcripts + sessions.

The re-extraction dedup (confirm_transcript) matches a pasted transcript against
{opportunity_id, transcript_hash}. Transcripts created before that field existed
have no hash, so a re-paste of an old interview would still create a duplicate.
This computes the hash from each transcript's stored raw_text and writes it onto
the transcript and its session, so dedup works against historical data too.

Dry-run by default; pass --apply to write.

    python scripts/backfill_transcript_hash.py
    python scripts/backfill_transcript_hash.py --apply
"""

import argparse
import asyncio
import hashlib
import os
import re
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app.db.collections import INTERVIEW_SESSIONS, TRANSCRIPTS  # noqa: E402


def transcript_hash(raw_text: str) -> str:
    normalized = re.sub(r"\s+", " ", (raw_text or "")).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def main(apply: bool) -> None:
    load_dotenv(os.path.join(_ROOT, ".env"))
    db = AsyncIOMotorClient(os.environ["MONGO_URI"])[os.environ["MONGO_DB_NAME"]]
    now = datetime.now(timezone.utc)

    done = 0
    skipped = 0
    async for transcript in db[TRANSCRIPTS].find({"transcript_hash": {"$exists": False}}):
        raw = transcript.get("raw_text")
        if not raw:
            skipped += 1
            continue
        thash = transcript_hash(raw)
        print(f"  transcript {transcript['_id']} -> {thash[:12]}...")
        if apply:
            await db[TRANSCRIPTS].update_one(
                {"_id": transcript["_id"]}, {"$set": {"transcript_hash": thash, "updated_at": now}}
            )
            if transcript.get("session_id"):
                await db[INTERVIEW_SESSIONS].update_one(
                    {"_id": transcript["session_id"]}, {"$set": {"transcript_hash": thash}}
                )
        done += 1

    print(f"\n{'APPLIED' if apply else 'DRY-RUN'}: {done} transcript(s) hashed, {skipped} skipped (no raw_text)")
    if not apply:
        print("Re-run with --apply to write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually write (default: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
