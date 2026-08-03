"""Seed / update the fixed set of RSA admins.

1. Edit the ADMINS list below (name + email for each admin).
2. Run:   python scripts/seed_admins.py
   - New admins are created with a one-time temporary password (printed once).
   - Existing admins (matched by email) are left untouched.
   - Pass --reset to re-issue a fresh temp password for existing admins too
     (use for "forgot password").

Share each admin their email + temp password. On first login they are forced to
set their own private password (mirrors the student flow). You never store or see
their real password.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from app.config.constants import ROLE_ADMIN  # noqa: E402
from app.services.auth_service import generate_temp_password  # noqa: E402
from app.utils.password import hash_password  # noqa: E402

# ---------------------------------------------------------------------------
# EDIT THIS FIXED LIST — one entry per admin.
# ---------------------------------------------------------------------------
ADMINS = [
    {"name": "Admin One", "email": "admin@example.com"},
    
    # add one entry per admin, then run this script
]


async def main(reset: bool) -> None:
    load_dotenv(os.path.join(_ROOT, ".env"))
    db = AsyncIOMotorClient(os.environ["MONGO_URI"])[os.environ["MONGO_DB_NAME"]]
    now = datetime.now(timezone.utc)

    print(f"{'email':32} {'temp_password':16} action")
    print("-" * 62)
    for entry in ADMINS:
        email = entry["email"].strip().lower()
        existing = await db["admins"].find_one({"email": email})
        if existing and not reset:
            print(f"{email:32} {'(unchanged)':16} skipped")
            continue

        temp = generate_temp_password()
        doc = {
            "name": entry["name"],
            "email": email,
            "role": ROLE_ADMIN,
            "password_hash": hash_password(temp),
            "is_password_set": False,
            "force_password_reset": True,
            "updated_at": now,
        }
        if existing:
            await db["admins"].update_one({"_id": existing["_id"]}, {"$set": doc})
            action = "reset"
        else:
            doc["created_at"] = now
            await db["admins"].insert_one(doc)
            action = "created"
        print(f"{email:32} {temp:16} {action}")

    print("\nShare each email + temp password. Admins set their own on first login.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed RSA admin accounts.")
    parser.add_argument("--reset", action="store_true",
                        help="re-issue a temp password for existing admins too")
    asyncio.run(main(parser.parse_args().reset))
