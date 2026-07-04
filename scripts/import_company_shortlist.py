import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.db.collections import COMPANIES, COMPANY_APPLICATIONS, COMPANY_SHORTLISTS
from app.db.indexes import create_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.services.student_service import normalize_email


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


def read_tsv(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t")
        for row in reader:
            normalized = {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
            if any(normalized.values()):
                rows.append(normalized)
    return rows


def pick(row: dict[str, str], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


async def find_company(company_name: str, role: str) -> dict:
    db = get_database()
    company = await db[COMPANIES].find_one({"company_key": key(company_name), "role_key": key(role)})
    if not company:
        raise RuntimeError(f"Company opportunity not found: {company_name} - {role}")
    return company


async def import_shortlist(args: argparse.Namespace) -> dict[str, int]:
    await connect_to_mongo()
    await create_indexes()
    db = get_database()

    company = await find_company(args.company_name, args.role)
    rows = read_tsv(Path(args.shortlist_sheet))

    inserted = updated = matched_applications = unmatched = skipped = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        email = normalize_email(pick(row, "Email"))
        full_name = pick(row, "Full Name", "Student Name", "Name")
        if not email and not full_name:
            skipped += 1
            continue

        application = None
        if email:
            application = await db[COMPANY_APPLICATIONS].find_one(
                {"company_id": company["_id"], "email": email}
            )

        if not application and not args.store_unmatched:
            unmatched += 1
            continue

        shortlist_doc = {
            "company_id": company["_id"],
            "company_name": args.company_name.strip(),
            "role": args.role.strip(),
            "student_id": application.get("student_id") if application else None,
            "application_id": application.get("_id") if application else None,
            "full_name": full_name,
            "email": email,
            "bachelors_course_name": pick(row, "Bachelors Course Name"),
            "bachelors_department_name": pick(row, "Bachelors Department Name"),
            "bachelors_year_of_completion": pick(row, "Bachelors Year of Completion"),
            "resume": pick(row, "Resume"),
            "resume_shortlisting": pick(row, "Resume Shortlisting"),
            "final_status": pick(row, "Final Status"),
            "status": args.status,
            "matched_application": bool(application),
            "raw_shortlist_row": row,
            "updated_at": now,
        }

        if email:
            result = await db[COMPANY_SHORTLISTS].update_one(
                {"company_id": company["_id"], "email": email},
                {"$set": shortlist_doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
            if result.upserted_id:
                inserted += 1
            else:
                updated += 1
        else:
            shortlist_doc["created_at"] = now
            await db[COMPANY_SHORTLISTS].insert_one(shortlist_doc)
            inserted += 1

        if application:
            await db[COMPANY_APPLICATIONS].update_one(
                {"_id": application["_id"]},
                {
                    "$set": {
                        "status": args.status,
                        "shortlisted_at": now,
                        "shortlist_resume": shortlist_doc["resume"],
                        "final_status": shortlist_doc["final_status"],
                        "updated_at": now,
                    }
                },
            )
            matched_applications += 1
        else:
            unmatched += 1

    await close_mongo_connection()
    return {
        "rows_read": len(rows),
        "shortlists_inserted": inserted,
        "shortlists_updated": updated,
        "applications_marked_shortlisted": matched_applications,
        "unmatched_shortlists": unmatched,
        "skipped": skipped,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import shortlisted candidates for a company opportunity.")
    parser.add_argument("--company-name", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--shortlist-sheet", required=True, help="Path to pasted TSV shortlist sheet text file.")
    parser.add_argument("--status", default="shortlisted")
    parser.add_argument(
        "--store-unmatched",
        action="store_true",
        help="Store shortlist rows that do not match an existing company application.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(asyncio.run(import_shortlist(parse_args())))
