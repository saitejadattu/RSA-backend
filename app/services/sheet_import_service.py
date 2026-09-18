"""Import pasted response / shortlist sheets against a known opportunity.

Some company sheets could never be downloaded (HTTP 401, bad URLs), so this is
the manual path: an admin pastes the sheet straight into the opportunity page.

The parsing here is the same logic the CLI scripts use - see
scripts/import_company_response.py and scripts/import_company_shortlist.py,
which now delegate to this module so the two paths cannot drift apart.

Unlike the CLI, the opportunity is already known, so none of the fragile
company-name/received-on resolution is needed.
"""
import csv
import difflib
import io
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any

import httpx
from bson import ObjectId
from fastapi import HTTPException, status
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.config.settings import get_settings
from app.db.collections import (
    APPLICATIONS,
    COMPANIES,
    HIRING_OPPORTUNITIES,
    STATUS_HISTORY,
    STUDENTS,
)
from app.db.mongodb import get_database
from app.models.application import (
    build_application_details,
    default_placement,
    final_status_for,
    normalize_application_status,
    status_for_api,
)
from app.models.student import build_student_document
from app.services.opportunity_counter_service import refresh_opportunity_counts
from app.services.student_service import normalize_email, normalize_phone
from app.utils.mongo import serialize_mongo
from app.utils.object_id import to_object_id
from app.utils.password import hash_password

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# Only an explicit refusal drops an applicant; a blank interest cell counts as
# applied (they are in the export because they applied).
NEGATIVE_INTEREST = {"no", "not interested", "not intrested", "n", "false", "0"}

# Statuses already past "applied". Re-importing a response sheet must not drag
# these back to APPLIED - the sheet only ever says someone applied, it knows
# nothing about interviews or offers that happened later.
AHEAD_OF_APPLIED = {
    "PROFILE_SHARED", "SHORTLISTED", "INTERVIEW_SCHEDULED", "INTERVIEW_IN_PROGRESS",
    "SELECTED", "OFFER_PENDING", "OFFER_RELEASED", "OFFER_ACCEPTED", "OFFER_REJECTED",
    "JOINED", "REJECTED", "DROPPED",
}


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _sniff_delimiter(sample: str) -> str:
    """Sheets paste as TSV; a CSV export is also accepted."""
    return "\t" if sample.count("\t") >= sample.count(",") else ","


# --------------------------------------------------------------------------
# response sheets - header based, because column wording differs per company
# --------------------------------------------------------------------------


def read_response_rows(raw_text: str) -> list[dict[str, str | None]]:
    lines = (raw_text or "").splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    delimiter = _sniff_delimiter("".join(lines[:3]))
    # newline="" so csv keeps newlines embedded in quoted cells (master-sheet
    # notes/questions span multiple lines) instead of erroring on them.
    reader = csv.DictReader(io.StringIO("".join(lines), newline=""), delimiter=delimiter)
    rows: list[dict[str, str | None]] = []
    for row in reader:
        normalized = {normalize_header(k or ""): clean(v) for k, v in row.items() if k}
        if any(normalized.values()):
            rows.append(normalized)
    return rows


def pick(row: dict[str, str | None], *aliases: str) -> str | None:
    for alias in aliases:
        value = row.get(normalize_header(alias))
        if value:
            return value
    return None


def pick_prefix(row: dict[str, str | None], *prefixes: str) -> str | None:
    """Match by header prefix, for columns whose text varies per opportunity
    (e.g. 'Are you willing to work in <Location>?')."""
    for prefix in prefixes:
        normalized_prefix = normalize_header(prefix)
        for header, value in row.items():
            if value and header.startswith(normalized_prefix):
                return value
    return None


def parse_rating(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    match = re.search(r"\d+", value)
    return {"score": int(match.group(0)) if match else None, "label": value}


def extract_skills(row: dict[str, str | None]) -> dict[str, dict[str, Any]]:
    """Skill columns sit between the 'interested' question and 'Do you have
    relevant project experience?'. Stop at that boundary so a stray rating
    column elsewhere in the sheet is ignored."""
    skills: dict[str, dict[str, Any]] = {}
    for header, value in row.items():
        if header == "do_you_have_relevant_project_experience":
            break
        match = re.match(r"skill_assessment_ratings_(.+)", header)
        if match and value:
            rating = parse_rating(value)
            if rating:
                skills[match.group(1).strip("_")] = rating
    return skills


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in (
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",  # ISO platform export
        "%B %d, %Y, %I:%M %p", "%B %d, %Y",                 # "July 24, 2026, 9:42 PM"
        "%d-%b-%Y", "%b-%d-%Y",
    ):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Our own sync checkpoints are stored as ISO 8601 ("2026-01-02T12:00:00Z").
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- resilient column detection -------------------------------------------
#
# Sheet exports rename columns and drop whole sections over time (a Google Form
# became a platform export: "Student Name" -> "Full Name", "Email" -> "Registered
# Email", "Mobile Number" -> "Registered Movbile Number", interest/skills gone).
# Matching by a fixed list of names silently imports nothing when that happens.
# Instead we DETECT each identity field: exact name, then keyword, then fuzzy -
# and the preview shows what mapped so a future change is loud, not silent.

FIELD_DETECT: dict[str, dict[str, tuple[str, ...]]] = {
    "uid": {
        "aliases": ("student_uid", "user_id", "student_id", "uid", "userid"),
        "keywords": ("uid", "user_id", "student_id"),
        "exclude": ("job", "company", "product"),
        "fuzzy": (),
    },
    "name": {
        # Full-name variants first so a "Full Name" column wins over a bare "Name".
        "aliases": ("full_name", "student_full_name", "candidate_full_name", "student_name",
                    "candidate_name", "applicant_name", "name"),
        "keywords": ("full_name", "student_name", "candidate_name", "applicant_name", "name"),
        # Don't grab company / college / job / file etc. name columns.
        "exclude": ("company", "college", "job", "role", "product", "file", "sheet",
                    "opportunity", "parent", "father", "mother", "guardian"),
        "fuzzy": (),
    },
    "email": {
        "aliases": ("email", "email_id", "registered_email", "email_address"),
        "keywords": ("email",),
        "exclude": (),
        "fuzzy": (),
    },
    "phone": {
        "aliases": (
            "mobile_number", "phone", "mobile", "registered_mobile_number",
            "registered_movbile_number", "contact_number", "phone_number", "whatsapp_number",
        ),
        "keywords": ("mobile", "movbile", "phone", "contact", "whatsapp"),
        "exclude": (),
        "fuzzy": ("mobile_number",),  # backstop for unseen typos
    },
    "resume": {
        "aliases": ("resume", "resume_link", "cv"),
        "keywords": ("resume", "cv"),
        "exclude": ("shortlisting",),  # "Resume Shortlisting" is a status, not the link
        "fuzzy": (),
    },
}


def _match_field(headers: list[str], rule: dict[str, tuple[str, ...]]) -> str | None:
    for alias in rule["aliases"]:  # 1. exact
        if alias in headers:
            return alias
    for header in headers:  # 2. keyword (respecting exclusions)
        if any(bad in header for bad in rule["exclude"]):
            continue
        if any(keyword in header for keyword in rule["keywords"]):
            return header
    best, best_ratio = None, 0.0  # 3. fuzzy backstop (typos)
    for target in rule["fuzzy"]:
        for header in headers:
            if any(bad in header for bad in rule["exclude"]):
                continue
            ratio = difflib.SequenceMatcher(None, header, target).ratio()
            if ratio > best_ratio:
                best, best_ratio = header, ratio
    return best if best_ratio >= 0.72 else None


def build_field_map(headers: list[str]) -> dict[str, str | None]:
    return {field: _match_field(headers, rule) for field, rule in FIELD_DETECT.items()}


def detect_interest_header(headers: list[str]) -> str | None:
    for header in headers:
        if "interested_in_applying" in header or "are_you_interested" in header:
            return header
    for header in headers:
        if "interested" in header and "why" not in header and "reason" not in header:
            return header
    return None


def prettify_header(header: str | None) -> str | None:
    """Normalized key -> a readable label for the preview mapping."""
    if not header:
        return None
    return header.replace("_", " ").strip().title()


def extract_identity(row: dict[str, str | None], field_map: dict[str, str | None]) -> dict[str, Any]:
    def val(field: str) -> str | None:
        header = field_map.get(field)
        return row.get(header) if header else None

    return {
        "uid": val("uid"),
        "name": val("name"),
        "email": normalize_email(val("email")),
        "phone": normalize_phone(val("phone") or ""),
        "resume": val("resume"),
    }


def build_application_fields(
    row: dict[str, str | None],
    *,
    opportunity: dict,
    company: dict,
    student_id,
    field_map: dict[str, str | None],
    interest_header: str | None,
    source: str = "response_sheet",
) -> dict:
    # Being in the response sheet means they applied. A blank interest cell just
    # means they didn't fill it - only an explicit "no" drops them. This is what
    # kept ~90% of a real opening wrongly DROPPED (the interest column existed
    # but was blank for most rows).
    if interest_header:
        val = (row.get(interest_header) or "").strip().lower()
        interested = val not in NEGATIVE_INTEREST
    else:
        interested = True
    current_status = normalize_application_status(None, interested=interested)
    resume_header = field_map.get("resume")
    return {
        "student_id": student_id,
        "company_id": company["_id"],
        "opportunity_id": opportunity["_id"],
        "applied_at": parse_timestamp(pick(row, "Timestamp", "Creation Datetime")),
        "source": source,
        "current_status": current_status,
        "final_status": final_status_for(current_status, interested=interested),
        "application_details": build_application_details(
            interested=interested,
            skills=extract_skills(row),
            has_relevant_project_experience=pick(row, "Do you have relevant project experience?"),
            github_link=pick(row, "GitHub Profile Link (Ensure it is public)", "GitHub Profile Link"),
            project_link=pick_prefix(row, "Project Link"),
            submitted_resume_url=(row.get(resume_header) if resume_header else None),
            willing_remote=pick_prefix(row, "Are you willing to work in"),
            available_full_duration=pick_prefix(row, "Are you available for the full"),
            comfortable_stipend=pick_prefix(row, "Are you comfortable with the stipend"),
            comfortable_schedule=pick_prefix(row, "Are you comfortable with the specified work schedule"),
            college_noc=pick(row, "Will your college allow you to proceed with this internship (NOC)?"),
            interest_reason=pick_prefix(row, "Why are you interested"),
            non_interest_reason=pick(row, "Reason (If NOT Applying) - Please select the primary reason for non-interest."),
            # The whole row is kept, so any column we don't map explicitly
            # (percentages, gender, program tier, ...) is never lost.
            other_response={
                "not_interested_other_reason": pick(row, "If 'Other' reason was selected, please specify:"),
                "raw_response": row,
            },
        ),
        "placement": default_placement(),
        "notes": None,
    }


def student_update_fields(row: dict[str, str | None], identity: dict) -> dict[str, Any]:
    return {
        "external_user_id": identity["uid"],
        "name": identity["name"],
        "phone": identity["phone"],
        "resume_link": identity["resume"],
        "current_city": pick(row, "Current City"),
        "college_name": pick(row, "College Name"),
        "degree": pick(row, "Degree (e.g., B.Tech, M.Tech, BCA, etc.)", "Degree", "Bachelors Course Name"),
        "department": pick(row, "Department (e.g., CSE, ECE, IT)", "Department", "Bachelors Department Name"),
        "year_of_passing": pick(row, "Year of Passing", "Bachelors Year of Graduation"),
        "technical_developer_name": pick(row, "Mention your Techincal Developer Name.", "Technical Developer Name"),
        "gender": pick(row, "Gender"),
    }


# --- company decision remarks ----------------------------------------------
#
# The Company Sheet now carries a per-candidate "remarks" dropdown: either a
# shortlist decision or a rejection reason the candidate should see and act on.

REMARK_HEADER_KEYWORDS = ("remark", "feedback", "final status", "final_status", "decision")
# Statuses at the screening stage - a remark may move a candidate between these,
# but must never pull back someone already in interviews or beyond.
SCREENING_STATUSES = {"APPLIED", "PROFILE_SHARED", "SHORTLISTED", "NOT_SHORTLISTED"}
LOCKED_FROM_DROP = {"JOINED", "OFFER_ACCEPTED"}


def detect_remark_header(headers: list[str]) -> str | None:
    for keyword in REMARK_HEADER_KEYWORDS:
        norm = normalize_header(keyword)
        for header in headers:
            # avoid "call status" / "message status" - only the decision column
            if header == norm or (norm in header and "call" not in header and "message" not in header):
                return header
    return None


def classify_remark(remark: str | None) -> tuple[str, str | None]:
    """A remark -> (decision, target_status_or_None).

    decision is one of: shortlisted, not_shortlisted, selected_elsewhere,
    waitlisted, resume_not_found, none. Only shortlisted / not_shortlisted /
    selected_elsewhere carry a target status; the rest only annotate.
    """
    low = (remark or "").strip().lower()
    if not low:
        return "none", None
    if "not shortlist" in low:
        # A rejection reason is shown to the student as feedback but does NOT
        # change status - they stay applied (only shortlist/selected move it).
        return "not_shortlisted", None
    if "shortlist" in low:
        return "shortlisted", "SHORTLISTED"
    if "selected to other" in low or "selected elsewhere" in low or "other company" in low:
        return "selected_elsewhere", "DROPPED"
    if "waitlist" in low:
        return "waitlisted", None
    if "resume" in low and "not" in low and ("found" in low or "notfound" in low):
        return "resume_not_found", None
    # Any other non-empty remark is a rejection reason (resume/projects/tech/profile).
    return "not_shortlisted", None


def apply_remark_status(current_status: str | None, target_status: str | None) -> str | None:
    """Set the status a remark implies, without dragging anyone backwards past
    the screening stage."""
    if not target_status:
        return current_status
    if target_status == "DROPPED":
        return current_status if current_status in LOCKED_FROM_DROP else "DROPPED"
    # SHORTLISTED / NOT_SHORTLISTED only apply while still at the screening stage.
    if (current_status or "APPLIED") in SCREENING_STATUSES:
        return target_status
    return current_status


# --------------------------------------------------------------------------
# shortlist sheets - positional, because the notes column is unlabeled and
# some rows are shifted (missing UID)
# --------------------------------------------------------------------------


def read_shortlist_rows(raw_text: str) -> list[list[str | None]]:
    lines = (raw_text or "").splitlines(keepends=True)
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return []
    delimiter = _sniff_delimiter("".join(lines[:3]))
    rows: list[list[str | None]] = []
    for cells in csv.reader(io.StringIO("".join(lines), newline=""), delimiter=delimiter):
        cleaned = [clean(c) for c in cells]
        if not any(cleaned):
            continue
        first = (cleaned[0] or "").strip().lower()
        # Header rows (they can repeat mid-sheet). Accept the many ways the name /
        # index column gets labelled so a "Student Name" header isn't read as data.
        if first in {
            "uid", "full name", "name", "student name", "student full name",
            "candidate name", "applicant name", "s.no", "sno", "s no", "sl.no",
            "sl no", "serial", "serial no", "#",
        }:
            continue
        rows.append(cleaned)
    return rows


def find_in_cells(cells: list[str | None], predicate) -> str | None:
    for cell in cells:
        if cell and predicate(cell):
            return cell
    return None


def looks_like_phone(cell: str) -> bool:
    digits = re.sub(r"\D", "", cell)
    return 10 <= len(digits) <= 13 and bool(re.fullmatch(r"[\d\s+()\-]+", cell.strip()))


def normalize_willing(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    if "not" in text and "interest" in text:
        return "not_interested"
    if "interest" in text:
        return "interested"
    return None


def extract_shortlist_row(cells: list[str | None]) -> dict[str, Any]:
    """Best-effort extraction robust to a missing or shifted UID column."""
    uid = find_in_cells(cells, lambda c: bool(UUID_RE.match(c)))
    email = normalize_email(find_in_cells(cells, lambda c: "@" in c))
    phone = normalize_phone(find_in_cells(cells, looks_like_phone) or "")
    resume = find_in_cells(cells, lambda c: c.lower().startswith("http"))
    willing_raw = find_in_cells(
        cells, lambda c: c.strip().lower() in ("interested", "not interested", "not intrested")
    )
    willing_index = cells.index(willing_raw) if willing_raw in cells else None
    notes = None
    if willing_index is not None:
        notes = " ".join(c for c in cells[willing_index + 1:] if c) or None
    call_status = find_in_cells(cells, lambda c: "call" in c.lower())
    call_date = find_in_cells(cells, lambda c: bool(re.match(r"\d{1,2}/\d{1,2}/\d{2,4}", c.strip())))
    name = None
    for cell in cells:
        if (
            cell and cell != uid and "@" not in cell
            and not cell.lower().startswith("http")
            and not re.fullmatch(r"[\d\s/:-]+", cell)
        ):
            name = cell
            break
    return {
        "uid": uid,
        "name": name,
        "email": email,
        "phone": phone,
        "resume": resume,
        "call_date": call_date,
        "call_status": call_status,
        "willing_to_join": normalize_willing(willing_raw),
        "willing_notes": notes,
        "raw_shortlist_row": cells,
    }


# --------------------------------------------------------------------------
# shared lookups
# --------------------------------------------------------------------------


async def find_student(db, identity: dict) -> dict | None:
    queries = []
    if identity.get("uid"):
        queries.append({"external_user_id": identity["uid"]})
    if identity.get("email"):
        queries.append({"email": identity["email"]})
    if identity.get("phone"):
        queries.append({"phone": identity["phone"]})
    if not queries:
        return None
    return await db[STUDENTS].find_one({"$or": queries})


def name_key(value: str | None) -> str:
    """Compare names ignoring case, punctuation and spacing.
    'A.mohamed yusuff' and 'Mohamed Yusuff' both -> 'amohamedyusuff' / 'mohamedyusuff'."""
    return re.sub(r"[^a-z]+", "", (value or "").lower())


def _name_matches(sheet_name: str | None, student_name: str | None) -> bool:
    a, b = name_key(sheet_name), name_key(student_name)
    if not a or not b or len(a) < 4 or len(b) < 4:
        return False
    if a == b:
        return True
    # One side often carries an initial or extra token the other omits
    # ("Sai Chaitanya" vs "Sai Chaitanya Reddy", "A.mohamed yusuff" vs "Mohamed yusuff").
    return a in b or b in a


async def build_applicant_index(db, opportunity_id) -> list[dict]:
    """Students who already have an application for THIS opening.

    A shortlist can only ever mark someone who applied here, so this set is the
    entire universe a shortlist row is matched against - by id or by name. That
    also removes the "same name, different person" risk that matching across all
    students would carry.
    """
    applications = await db[APPLICATIONS].find(
        {"opportunity_id": opportunity_id}, {"student_id": 1}
    ).to_list(length=None)
    student_ids = [application["student_id"] for application in applications]
    if not student_ids:
        return []
    return await db[STUDENTS].find(
        {"_id": {"$in": student_ids}},
        {"name": 1, "email": 1, "phone": 1, "external_user_id": 1, "resume_link": 1},
    ).to_list(length=None)


_RESUME_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def resume_key(url: str | None) -> str | None:
    """A stable identity key for a resume link. Prefers the trailing UUID (same
    file, regardless of URL encoding); falls back to the normalized URL."""
    if not url:
        return None
    low = str(url).strip().lower()
    if not low:
        return None
    match = _RESUME_UUID_RE.search(low)
    return match.group(0) if match else low


def match_applicant(data: dict, applicants: list[dict]) -> tuple[dict | None, bool]:
    """Find this shortlist row among the opening's applicants.

    Identity fields first (exact), then name. Returns (student, ambiguous).
    """
    for field, value in (
        ("external_user_id", data.get("uid")),
        ("email", data.get("email")),
        ("phone", data.get("phone")),
    ):
        if not value:
            continue
        for student in applicants:
            if student.get(field) and student[field] == value:
                return student, False
    # A resume link is a strong unique identifier - use it before falling back to
    # the name, so two applicants sharing a first name (e.g. "Nandhini") resolve
    # to the right person when the shortlist sheet carries a resume.
    wanted_resume = resume_key(data.get("resume"))
    if wanted_resume:
        hits = [s for s in applicants if resume_key(s.get("resume_link")) == wanted_resume]
        if len(hits) == 1:
            return hits[0], False
    return match_by_name(data.get("name"), applicants)


def match_by_name(sheet_name: str | None, applicants: list[dict]) -> tuple[dict | None, bool]:
    """Return (student, ambiguous). Ambiguous means several applicants share the
    name, so the admin must resolve it rather than us guessing."""
    hits = [student for student in applicants if _name_matches(sheet_name, student.get("name"))]
    if len(hits) == 1:
        return hits[0], False
    if len(hits) > 1:
        return None, True
    return None, False


SHEET_ID_RE = re.compile(r"/d/([\w-]+)")
SHEET_GID_RE = re.compile(r"gid=(\d+)")


def sheet_export_url(url: str | None) -> str | None:
    """A Google Sheets URL -> its public TSV export URL, preserving the tab
    (gid) so the shortlist link points at the shortlist tab, not the first one."""
    if not url:
        return None
    doc = SHEET_ID_RE.search(url)
    if not doc:
        return None
    gid = SHEET_GID_RE.search(url)
    return f"https://docs.google.com/spreadsheets/d/{doc.group(1)}/export?format=tsv&gid={gid.group(1) if gid else '0'}"


async def fetch_sheet_text(url: str | None) -> str:
    """Fetch a sheet as TSV with no auth. Works only for 'anyone with the link'
    sheets - a restricted one returns an HTML sign-in page, which we turn into a
    clear message telling the admin to share it or paste instead."""
    export = sheet_export_url(url)
    if not export:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="That doesn't look like a Google Sheets link.",
        )
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(export)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach Google Sheets: {exc}")

    content_type = response.headers.get("content-type", "").lower()
    if response.status_code != 200 or "html" in content_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This sheet isn't shared publicly, so it can't be fetched. Set its link sharing to "
                "'Anyone with the link - Viewer', or paste the data in the tab above instead."
            ),
        )
    if not response.text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The sheet came back empty.")
    return response.text


async def fetch_response_incremental_text(url: str, start_row: int) -> str:
    """Fetch the response header and only rows after the saved source-row watermark."""
    export = sheet_export_url(url)
    if not export:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Response incremental sync failed: that doesn't look like a Google Sheets link.",
        )
    document = SHEET_ID_RE.search(url)
    gid_match = SHEET_GID_RE.search(url)
    if not document:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid response sheet URL")
    gviz = f"https://docs.google.com/spreadsheets/d/{document.group(1)}/gviz/tq"
    params = {"tqx": "out:tsv", "gid": gid_match.group(1) if gid_match else "0"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            header_response = await client.get(gviz, params={**params, "range": "A1:ZZ1"})
            rows_response = await client.get(gviz, params={**params, "range": f"A{start_row}:ZZ"})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Response incremental sync failed: {exc}") from exc

    for response in (header_response, rows_response):
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code != 200 or "html" in content_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Response incremental sync failed: the sheet is not publicly accessible.",
            )
    if not header_response.text.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Response incremental sync failed: header row is empty.")
    if not rows_response.text.strip():
        return header_response.text
    return f"{header_response.text.rstrip(chr(10))}\n{rows_response.text.lstrip(chr(10))}"


def _response_checkpoint(row: dict[str, str | None], row_index: int) -> tuple[datetime | None, int, str | None]:
    """Return (timestamp, source_row, uid) for a response row.

    The Google Forms timestamp is the safest per-response marker available in the
    sheet data. We keep the row number as a tie-breaker only when multiple rows
    share the exact same timestamp, which prevents a timestamp-only cursor from
    skipping records.
    """
    ts_value = pick(row, "Timestamp", "Creation Datetime", "Submitted At")
    ts = parse_timestamp(ts_value)
    uid = pick(row, "Student UID", "student_uid", "uid", "User ID")
    return ts, row_index, uid


def _rebuild_raw_text(headers: list[str], rows: list[dict[str, str | None]]) -> str:
    if not headers or not rows:
        return ""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({header: row.get(header, "") for header in headers})
    return output.getvalue()


async def sync_response_sheet_incremental(*, opportunity_id: str) -> dict:
    """Process only response rows newer than the opportunity's saved checkpoint.

    The response sheet exposes a Google Forms Timestamp column, which is a safer
    per-response cursor than a raw row number. We still keep the last processed
    source row as a tie-breaker when two submissions share the same timestamp.
    """
    db = get_database()
    opportunity, _ = await load_opportunity(db, opportunity_id)
    url = (opportunity.get("student_response_sheet") or "").strip()
    if not url:
        return serialize_mongo({
            "mode": "incremental",
            "opportunity_id": opportunity_id,
            "rows_scanned": 0,
            "rows_processed": 0,
            "applications_created": 0,
            "applications_updated": 0,
            "skipped": 0,
            "message": "No response sheet URL is stored for this opportunity.",
        })

    response_sync = opportunity.get("response_sync") or {}
    last_timestamp = response_sync.get("last_processed_response_timestamp")
    last_row = response_sync.get("last_processed_row")
    if (not last_timestamp and last_row is None) or link_changed_since_import(opportunity, "responses"):
        # No usable checkpoint - never imported, imported before checkpoints were
        # recorded, or the link now points at a different sheet: run the normal
        # full import, which records the checkpoint for next time.
        return await sync_from_sheet(opportunity_id=opportunity_id, kind="responses", confirm=True, force=True)

    raw_text = await fetch_sheet_text(url)
    all_rows = read_response_rows(raw_text)
    if not all_rows:
        return serialize_mongo({
            "mode": "incremental",
            "opportunity_id": opportunity_id,
            "rows_scanned": 0,
            "rows_processed": 0,
            "applications_created": 0,
            "applications_updated": 0,
            "skipped": 0,
            "last_processed_response_timestamp": last_timestamp,
            "last_processed_row": last_row,
            "message": "No new response rows were found after the saved checkpoint.",
        })

    headers = list(all_rows[0].keys())
    new_rows: list[dict[str, str | None]] = []
    latest_seen: tuple[datetime | None, int, str | None] | None = None
    for raw_index, row in enumerate(all_rows, start=2):
        candidate_ts, candidate_row, candidate_uid = _response_checkpoint(row, raw_index)
        if not candidate_ts:
            continue
        if last_timestamp:
            last_dt = parse_timestamp(last_timestamp)
            if candidate_ts < (last_dt or candidate_ts):
                continue
            if candidate_ts == last_dt:
                if last_row is not None and candidate_row <= int(last_row):
                    continue
        elif last_row is not None and candidate_row <= int(last_row):
            continue
        new_rows.append(row)
        latest_seen = (candidate_ts, candidate_row, candidate_uid)

    if not new_rows:
        return serialize_mongo({
            "mode": "incremental",
            "opportunity_id": opportunity_id,
            "rows_scanned": len(all_rows),
            "rows_processed": 0,
            "applications_created": 0,
            "applications_updated": 0,
            "skipped": len(all_rows),
            "last_processed_response_timestamp": last_timestamp,
            "last_processed_row": last_row,
            "message": "No new response rows were found after the saved checkpoint.",
        })

    result = await import_responses(
        opportunity_id=opportunity_id,
        raw_text=_rebuild_raw_text(headers, new_rows),
        confirm=True,
        replace=False,
    )
    counts = result.get("counts", {})
    if latest_seen is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Response sync checkpoint could not be advanced.")
    latest_ts, latest_row, _ = latest_seen
    now = datetime.now(timezone.utc)
    next_sync = {
        **response_sync,
        "last_processed_response_timestamp": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else None,
        "last_processed_row": int(latest_row),
        "last_processed_at": now,
        "last_successful_sync_at": now,
    }
    await db[HIRING_OPPORTUNITIES].update_one({"_id": opportunity["_id"]}, {"$set": {"response_sync": next_sync}})

    return serialize_mongo({
        "mode": "incremental",
        "opportunity_id": opportunity_id,
        "rows_scanned": len(new_rows),
        "rows_processed": counts.get("applications_to_create", 0) + counts.get("applications_to_update", 0),
        "applications_created": counts.get("applications_to_create", 0),
        "applications_updated": counts.get("applications_to_update", 0),
        "skipped": counts.get("skipped", 0),
        "last_processed_response_timestamp": next_sync["last_processed_response_timestamp"],
        "last_processed_row": int(latest_row),
        "source_url": url,
        "result": result,
    })


# kind -> (sheet URL field, imported-at stamp, link-changed stamp, name used in messages)
SHEET_KINDS = {
    "responses": ("student_response_sheet", "responses_imported_at", "response_sheet_changed_at", "response"),
    "shortlist": ("company_sheet", "shortlist_imported_at", "company_sheet_changed_at", "company / shortlist"),
}


def link_changed_since_import(opportunity: dict, kind: str) -> bool:
    """A link that changed after the last import points at a corrected sheet,
    which should be pulled in full rather than skipped or read incrementally."""
    _, stamp_field, changed_field, _ = SHEET_KINDS[kind]
    stamp, changed_at = opportunity.get(stamp_field), opportunity.get(changed_field)
    return bool(changed_at and (not stamp or changed_at > stamp))


def is_empty_sheet(raw_text: str) -> bool:
    """Only a header, or nothing: the form / company sheet exists but nobody is on it yet."""
    text = raw_text or ""
    records = csv.reader(io.StringIO(text, newline=""), delimiter=_sniff_delimiter(text[:2000]))
    return sum(1 for record in records if any((cell or "").strip() for cell in record)) <= 1


def _response_sheet_checkpoint(raw_text: str) -> dict | None:
    """The checkpoint after a full response import - the newest submission and
    its row - so Pull only new continues from there. None when the sheet has no
    timestamps; every sync then stays a full import, which is still correct."""
    latest = None
    for row_index, row in enumerate(read_response_rows(raw_text), start=2):
        submitted_at, source_row, _ = _response_checkpoint(row, row_index)
        if submitted_at and (latest is None or submitted_at >= latest[0]):
            latest = (submitted_at, source_row)
    if latest is None:
        return None
    now = datetime.now(timezone.utc)
    return {
        "last_processed_response_timestamp": latest[0].isoformat().replace("+00:00", "Z"),
        "last_processed_row": latest[1],
        "last_processed_at": now,
        "last_successful_sync_at": now,
    }


async def import_fetched_sheet(
    db, opportunity: dict, kind: str, raw_text: str, url: str, *, confirm: bool, replace: bool = False
) -> dict:
    """Import a downloaded response / shortlist sheet - the one path every sync uses."""
    if is_empty_sheet(raw_text):
        return serialize_mongo({
            "mode": "skipped",
            "kind": kind,
            "source_url": url,
            "message": f"The {SHEET_KINDS[kind][3]} sheet has no rows yet.",
        })
    opportunity_id = str(opportunity["_id"])
    if kind == "responses":
        result = await import_responses(
            opportunity_id=opportunity_id, raw_text=raw_text, confirm=confirm, replace=replace
        )
        checkpoint = _response_sheet_checkpoint(raw_text) if confirm else None
        if checkpoint:
            await db[HIRING_OPPORTUNITIES].update_one({"_id": opportunity["_id"]}, {"$set": {"response_sync": checkpoint}})
    else:
        result = await import_shortlist(opportunity_id=opportunity_id, raw_text=raw_text, confirm=confirm)
    result["source_url"] = url
    return result


async def sync_from_sheet(
    *, opportunity_id: str, kind: str, confirm: bool = False, force: bool = False, replace: bool = False
) -> dict:
    """Fetch the opening's stored sheet URL and run the matching import.

    kind='responses' pulls Student Response Sheet -> import_responses.
    kind='shortlist' pulls Company Sheet          -> import_shortlist.
    Response must be synced before shortlist so the shortlist can match names.

    Already-extracted openings are skipped unless force=True - a URL is often
    fixed after a wrong/no-access sheet was linked, and we don't want to re-pull
    the ones already done every time.
    """
    if kind not in SHEET_KINDS:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="kind must be responses or shortlist")
    db = get_database()
    opportunity, _ = await load_opportunity(db, opportunity_id)
    url_field, stamp_field, _, missing = SHEET_KINDS[kind]
    url = opportunity.get(url_field)
    stamp = opportunity.get(stamp_field)

    # Skip an already-imported opening only if its link hasn't changed since.
    if stamp and not force and not link_changed_since_import(opportunity, kind):
        return serialize_mongo({
            "mode": "skipped",
            "kind": kind,
            "already_imported_at": stamp,
            "message": f"{kind.capitalize()} already imported for this opening. Use Force to re-import.",
        })

    if not (url or "").strip():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No {missing} sheet URL is stored on this opening. Add it via the master sheet, or paste instead.",
        )

    raw_text = await fetch_sheet_text(url)
    return await import_fetched_sheet(db, opportunity, kind, raw_text, url, confirm=confirm, replace=replace)


async def auto_sync_response_and_shortlist(*, opportunity_id: str) -> dict:
    """Import responses first, then import the configured shortlist sheet."""
    try:
        response_result = await sync_from_sheet(
            opportunity_id=opportunity_id,
            kind="responses",
            confirm=True,
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=(
                "Response sheet import failed. "
                "Shortlist import was not started. "
                f"{exc.detail}"
            ),
        ) from exc

    try:
        shortlist_result = await sync_from_sheet(
            opportunity_id=opportunity_id,
            kind="shortlist",
            confirm=True,
            # Automatic response fetches must re-run shortlist reconciliation;
            # an existing import stamp must not turn this step into a no-op.
            force=True,
        )
    except HTTPException as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=(
                "Response sheet imported successfully, but shortlist import failed: "
                f"{exc.detail}"
            ),
        ) from exc

    return serialize_mongo({
        "mode": "applied",
        "response": response_result,
        "shortlist": shortlist_result,
        "counts": response_result.get("counts", {}),
        "shortlist_counts": shortlist_result.get("counts", {}),
    })


async def update_sheet_links(
    *, opportunity_id: str, response_url: str | None = None, company_url: str | None = None
) -> dict:
    """Set / correct the response and/or shortlist sheet URL on one opening,
    without re-importing the whole master sheet.

    Each provided URL is validated as a Google Sheets link, stored, and stamped
    as 'changed now' so the next Sync pulls it (instead of skipping an already
    imported opening). Pass an empty string to clear a link.
    """
    db = get_database()
    opportunity, _ = await load_opportunity(db, opportunity_id)
    now = datetime.now(timezone.utc)

    set_fields: dict[str, Any] = {}
    for value, field, changed_at, previous in (
        (response_url, "student_response_sheet", "response_sheet_changed_at", "previous_student_response_sheet"),
        (company_url, "company_sheet", "company_sheet_changed_at", "previous_company_sheet"),
    ):
        if value is None:  # field not part of this update
            continue
        new_url = value.strip()
        if new_url and not sheet_export_url(new_url):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="That doesn't look like a Google Sheets link. Paste the full sheet URL.",
            )
        old_url = (opportunity.get(field) or "").strip()
        if new_url == old_url:
            continue
        set_fields[field] = new_url or None
        set_fields[changed_at] = now
        if old_url:
            set_fields[previous] = old_url

    if not set_fields:
        return serialize_mongo({"mode": "unchanged", "opportunity_id": opportunity_id})

    set_fields["updated_at"] = now
    await db[HIRING_OPPORTUNITIES].update_one({"_id": opportunity["_id"]}, {"$set": set_fields})
    updated = await db[HIRING_OPPORTUNITIES].find_one(
        {"_id": opportunity["_id"]},
        {"student_response_sheet": 1, "company_sheet": 1,
         "response_sheet_changed_at": 1, "company_sheet_changed_at": 1,
         "responses_imported_at": 1, "shortlist_imported_at": 1},
    )
    return serialize_mongo({"mode": "updated", **(updated or {})})


async def load_opportunity(db, opportunity_id: str) -> tuple[dict, dict]:
    try:
        object_id = to_object_id(opportunity_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid opportunity id")
    opportunity = await db[HIRING_OPPORTUNITIES].find_one({"_id": object_id})
    if not opportunity:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    company = await db[COMPANIES].find_one({"_id": opportunity["company_id"]})
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return opportunity, company


def _keep_status(existing: dict | None, incoming: str) -> str:
    """A response sheet only ever proves someone applied. If the pipeline has
    already moved past that, keep where they are."""
    current = (existing or {}).get("current_status")
    return current if current in AHEAD_OF_APPLIED else incoming


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------


async def import_responses(
    *, opportunity_id: str, raw_text: str, confirm: bool = False, replace: bool = False
) -> dict:
    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    rows = read_response_rows(raw_text)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No rows found. Paste the sheet including its header row.",
        )

    # Detect columns instead of hard-matching names, and surface the mapping so
    # a future format change is visible rather than silently importing nothing.
    headers = list(rows[0].keys())
    field_map = build_field_map(headers)
    interest_header = detect_interest_header(headers)
    # A response sheet may also carry the company's per-candidate remark; capture
    # it so applied students see their feedback.
    remark_header = detect_remark_header(headers)
    column_mapping = {field: prettify_header(header) for field, header in field_map.items()}
    column_mapping["interested"] = prettify_header(interest_header)
    column_mapping["remark"] = prettify_header(remark_header)

    # We can identify a student by uid, by email, or by name+phone (needed to
    # create). Without any of these the whole sheet is unusable - block loudly.
    can_identify = bool(field_map["uid"] or field_map["email"] or (field_map["name"] and field_map["phone"]))
    if not can_identify:
        missing = [f for f in ("uid", "email", "name", "phone") if not field_map[f]]
        message = (
            "Couldn't find the columns needed to identify students "
            f"(missing: {', '.join(missing)}). The sheet's format may have changed - "
            "check the column mapping below."
        )
        if confirm:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=message)
        return serialize_mongo({
            "mode": "preview",
            "blocked": True,
            "message": message,
            "column_mapping": column_mapping,
            "detected_headers": headers,
            "counts": {"rows": len(rows)},
            "rows": [],
        })

    now = datetime.now(timezone.utc)
    preview: list[dict[str, Any]] = []
    counts = {
        "rows": len(rows),
        "students_matched": 0,
        "students_to_create": 0,
        "applications_to_create": 0,
        "applications_to_update": 0,
        "status_preserved": 0,
        "skipped": 0,
    }
    # Replace mode: after importing the corrected sheet, any response-sourced
    # application on this opening whose student is NOT in the new sheet is stale
    # (a leftover from a wrong sheet). Collect the sheet's students to find them.
    sheet_student_ids: set = set()
    # A response sheet often has the same student on several rows (re-submissions).
    # Confirm dedupes those via upsert, so the preview must too or it reports
    # more creates than actually happen.
    seen_students: set[str] = set()

    for index, row in enumerate(rows, start=1):
        identity = extract_identity(row, field_map)
        entry: dict[str, Any] = {
            "row": index,
            "name": identity["name"],
            "email": identity["email"],
            "phone": identity["phone"],
        }

        if not identity["name"] or not identity["phone"]:
            entry["action"] = "skip"
            entry["reason"] = "Row needs at least a name and a mobile number."
            counts["skipped"] += 1
            preview.append(entry)
            continue

        student = await find_student(db, identity)
        row_key = str(student["_id"]) if student else (identity["uid"] or identity["email"] or identity["phone"])
        repeat = row_key in seen_students  # same person seen earlier in this sheet
        if row_key:
            seen_students.add(row_key)

        entry["student_exists"] = bool(student)
        if student:
            sheet_student_ids.add(student["_id"])
            counts["students_matched"] += 1
        elif not repeat:
            counts["students_to_create"] += 1

        existing_application = None
        if student:
            existing_application = await db[APPLICATIONS].find_one(
                {"opportunity_id": opportunity["_id"], "student_id": student["_id"]}
            )
        # A repeat row updates the record the earlier row already accounted for.
        is_update = bool(existing_application) or repeat

        if interest_header:
            interested = (row.get(interest_header) or "").strip().lower() not in NEGATIVE_INTEREST
        else:
            interested = True
        remark = (row.get(remark_header) or "").strip() if remark_header else ""
        decision, remark_target = classify_remark(remark)
        # The response sheet is NOT authoritative for shortlisting - the shortlist
        # (company) sheet is. A "Shortlisted" remark here must not promote the
        # candidate; they stay APPLIED until the shortlist import confirms them.
        # Drop it as visible feedback too, so an applied student isn't shown a
        # "shortlisted" note that the real shortlist may not back up.
        if decision == "shortlisted":
            decision, remark_target, remark = "none", None, ""
        # Base is applied (from interest); a "Selected elsewhere" remark can drop
        # it, but a rejection reason leaves it applied.
        incoming_status = remark_target or normalize_application_status(None, interested=interested)
        kept_status = _keep_status(existing_application, incoming_status)
        entry["action"] = "update" if is_update else "create"
        entry["status"] = kept_status
        if remark:
            entry["remark"] = remark
            entry["decision"] = decision
        if existing_application and kept_status != incoming_status:
            entry["status_preserved_from"] = existing_application.get("current_status")
            counts["status_preserved"] += 1
        counts["applications_to_update" if is_update else "applications_to_create"] += 1

        if not confirm:
            preview.append(entry)
            continue

        # ---- write ----
        if student:
            update_fields = {k: v for k, v in student_update_fields(row, identity).items() if v is not None}
            if identity["email"]:
                update_fields["email"] = identity["email"]
            update_fields["updated_at"] = now
            await db[STUDENTS].update_one({"_id": student["_id"]}, {"$set": update_fields})
            student_id = student["_id"]
        else:
            document = build_student_document(
                external_user_id=identity["uid"], name=identity["name"], email=identity["email"],
                phone=identity["phone"], stack=None, resume_link=identity["resume"],
                password_hash=hash_password(identity["phone"]),
            )
            document.update({k: v for k, v in student_update_fields(row, identity).items() if v is not None})
            try:
                student_id = (await db[STUDENTS].insert_one(document)).inserted_id
            except DuplicateKeyError:
                # Openings sync side by side, so another opening's import may have
                # created this student a moment ago - use that record.
                student = await find_student(db, identity)
                if not student:
                    raise
                student_id = student["_id"]

        fields = build_application_fields(
            row, opportunity=opportunity, company=company, student_id=student_id,
            field_map=field_map, interest_header=interest_header, source="response_paste",
        )
        existing_application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student_id}
        )
        # Apply the remark's status move (shortlisted / selected elsewhere) on top
        # of the interest-derived status, then guard against downgrades.
        fields["current_status"] = _keep_status(existing_application, remark_target or fields["current_status"])
        fields["final_status"] = final_status_for(
            fields["current_status"], interested=fields["application_details"].get("interested")
        )
        if remark:
            # The company's per-candidate feedback, shown to the student directly.
            fields["screening"] = {
                "remark": remark,
                "decision": decision,
                "source": "response_sheet",
                "imported_at": now,
                "visible_to_student": True,
            }

        if existing_application:
            await db[APPLICATIONS].update_one(
                {"_id": existing_application["_id"]}, {"$set": {**fields, "updated_at": now}}
            )
        else:
            fields["created_at"] = now
            fields["updated_at"] = now
            result = await db[APPLICATIONS].insert_one(fields)
            await db[STATUS_HISTORY].insert_one({
                "application_id": result.inserted_id,
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": None,
                "new_status": fields["current_status"],
                "reason": "Application imported from pasted response sheet",
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "response_paste",
                "created_at": now,
            })
        preview.append(entry)

    # ---- replace: reconcile stale candidates -------------------------------
    # The corrected response sheet is the source of truth for who applied. Any
    # response-sourced app on this opening whose student isn't in it is stale -
    # a leftover from a wrong sheet - even if it was already shortlisted, because
    # that student never really applied. Those are removed after a backup.
    # Only genuinely-hired stages (selected / offer / joined) are held back and
    # flagged for manual review, since auto-deleting a hired record is too risky.
    # Students are never deleted - only the application row.
    if replace:
        protected = {
            "SELECTED", "OFFER_PENDING", "OFFER_RELEASED",
            "OFFER_ACCEPTED", "OFFER_REJECTED", "JOINED",
        }
        counts["stale_removed"] = 0
        counts["stale_flagged"] = 0
        stale_cursor = db[APPLICATIONS].find({
            "opportunity_id": opportunity["_id"],
            "source": {"$in": ["response_paste", "response_sheet"]},
            "student_id": {"$nin": list(sheet_student_ids)},
        })
        async for stale in stale_cursor:
            student_doc = await db[STUDENTS].find_one(
                {"_id": stale["student_id"]}, {"name": 1, "email": 1}
            )
            cur = stale.get("current_status")
            hired = cur in protected
            entry = {
                "row": "-",
                "name": (student_doc or {}).get("name"),
                "email": (student_doc or {}).get("email"),
                "action": "needs_review" if hired else "remove",
                "status": cur,
                "reason": (
                    f"Not in the corrected sheet, but already {cur} - kept for manual review"
                    if hired else
                    "Not in the corrected sheet - stale candidate"
                ),
            }
            preview.append(entry)
            if hired:
                counts["stale_flagged"] += 1
                if confirm:
                    await db[APPLICATIONS].update_one(
                        {"_id": stale["_id"]},
                        {"$set": {"needs_review": {
                            "reason": "not_in_corrected_sheet",
                            "flagged_at": now,
                        }, "updated_at": now}},
                    )
            else:
                counts["stale_removed"] += 1
                if confirm:
                    await db["applications_removed_backup"].insert_one({
                        **stale,
                        "removed_at": now,
                        "removed_reason": "replace: not in corrected response sheet",
                        "removed_from_opportunity": opportunity["_id"],
                    })
                    await db[APPLICATIONS].delete_one({"_id": stale["_id"]})

    # Record that this opening's responses have been extracted, so a later bulk
    # sync can skip it (unless forced). Only stamp on a real, non-empty import.
    if confirm and (counts["applications_to_create"] or counts["applications_to_update"]):
        await db[HIRING_OPPORTUNITIES].update_one(
            {"_id": opportunity["_id"]},
            {"$set": {"responses_imported_at": now, "responses_row_count": counts["rows"]}},
        )

    return serialize_mongo({
        "mode": "applied" if confirm else "preview",
        "company": company.get("name"),
        "role": opportunity.get("role"),
        "column_mapping": column_mapping,
        "counts": counts,
        "rows": preview,
    })


# --------------------------------------------------------------------------
# shortlist
# --------------------------------------------------------------------------


# Screening-stage statuses a shortlist decision is allowed to overwrite. An
# interview/offer/joined/dropped status is a later, decided state and is left
# alone. NOT_SHORTLISTED is included so a re-import re-affirms it idempotently.
_SCREENING_STAGE = ["APPLIED", "PROFILE_SHARED", "SHORTLISTED", "NOT_SHORTLISTED"]


async def _reconcile_shortlist(db, *, opportunity, company, shortlisted_ids, now, confirm) -> int:
    """Once a shortlist is imported it is authoritative for the screening stage:
    every applicant on this opening who is NOT on the sheet is set to
    NOT_SHORTLISTED (this is also how a re-import corrects a wrong earlier
    shortlist). Response-sheet waitlist remarks do not override the authoritative
    shortlist, and interview/offer stages are never pulled back. Runs only when
    the sheet shortlisted at least one person, so an empty or failed pull can't
    blank a shortlist.

    Returns the count of applications changed (or that would be, in preview).
    """
    if not shortlisted_ids:
        return 0
    stale = await db[APPLICATIONS].find(
        {
            "opportunity_id": opportunity["_id"],
            "current_status": {"$in": ["APPLIED", "PROFILE_SHARED", "SHORTLISTED"]},
            "student_id": {"$nin": list(shortlisted_ids)},
        }
    ).to_list(length=None)
    if not confirm:
        return len(stale)
    for app in stale:
        old_status = app.get("current_status")
        update = {
            "$set": {
                "current_status": "NOT_SHORTLISTED",
                "final_status": final_status_for("NOT_SHORTLISTED", interested=True),
                "updated_at": now,
            }
        }
        if old_status == "SHORTLISTED":
            update["$unset"] = {"shortlisted_at": ""}
        await db[APPLICATIONS].update_one({"_id": app["_id"]}, update)
        await db[STATUS_HISTORY].insert_one({
            "application_id": app["_id"],
            "student_id": app.get("student_id"),
            "company_id": company["_id"],
            "opportunity_id": opportunity["_id"],
            "old_status": old_status,
            "new_status": "NOT_SHORTLISTED",
            "reason": "Not on the imported shortlist for this opening",
            "notes": None,
            "changed_by": None,
            "changed_by_role": "admin",
            "source": "shortlist_reconcile",
            "created_at": now,
        })
    return len(stale)


async def _import_company_decisions(
    *, opportunity_id: str, rows: list[dict[str, str | None]], field_map: dict[str, str | None], confirm: bool,
    reconcile: bool = True, update_opportunity: bool = True,
) -> dict:
    """Header-based company sheet: apply each candidate's remark decision and
    store the remark so the candidate can see why they weren't shortlisted.

    Matches only students who already applied to this opening; never creates.
    """
    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    now = datetime.now(timezone.utc)
    headers = list(rows[0].keys())
    remark_header = detect_remark_header(headers)
    applicants = await build_applicant_index(db, opportunity["_id"])

    preview: list[dict[str, Any]] = []
    shortlisted_ids: set = set()
    source_record_ids: set[str] = set()
    counts = {
        "rows": len(rows),
        "students_matched": 0,
        "shortlisted": 0,
        "not_shortlisted": 0,
        "selected_elsewhere": 0,
        "waitlisted": 0,
        "other": 0,
        "unmatched": 0,
        "no_remark": 0,
        "removed_from_shortlist": 0,
    }

    for index, row in enumerate(rows, start=1):
        identity = extract_identity(row, field_map)
        if identity.get("uid"):
            source_record_ids.add(identity["uid"])
        remark = (row.get(remark_header) or "").strip() if remark_header else ""
        decision, target = classify_remark(remark)
        # The company sheet IS the shortlist: a listed candidate with no explicit
        # decision is shortlisted by default. An explicit remark (not shortlisted
        # / selected elsewhere / waitlisted) overrides that.
        if decision == "none":
            decision, target = "shortlisted", "SHORTLISTED"
        entry: dict[str, Any] = {
            "row": index,
            "name": identity["name"],
            "email": identity["email"],
            "remark": remark or None,
            "decision": decision,
        }

        student, ambiguous = match_applicant(identity, applicants)
        if ambiguous or not student:
            entry["action"] = "skip"
            entry["reason"] = (
                "Several applicants match this name - add an email." if ambiguous
                else "This candidate hasn't applied to this opening. Import the response sheet first."
            )
            counts["unmatched"] += 1
            preview.append(entry)
            continue

        counts["students_matched"] += 1
        application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student["_id"]}
        )
        if not application:
            entry["action"] = "skip"
            entry["reason"] = "No application on this opening for this student."
            counts["unmatched"] += 1
            preview.append(entry)
            continue

        new_status = apply_remark_status(application.get("current_status"), target)
        entry["action"] = decision if decision != "none" else "no_change"
        entry["status"] = new_status
        if decision == "none":
            counts["no_remark"] += 1
        else:
            counts[decision] = counts.get(decision, 0) + 1
        # Everyone this sheet shortlists forms the authoritative set for the
        # reconcile below; anyone SHORTLISTED but absent from it gets demoted.
        if decision == "shortlisted":
            shortlisted_ids.add(student["_id"])

        if not confirm:
            preview.append(entry)
            continue

        old_status = application.get("current_status")
        update = {
            "screening": {
                "remark": remark or None,
                "decision": decision,
                "source": "company_sheet",
                "imported_at": now,
                # These are the company's own written decisions, shown to the
                # student directly (no separate review gate).
                "visible_to_student": True,
            },
            "updated_at": now,
        }
        if new_status and new_status != old_status:
            update["current_status"] = new_status
            update["final_status"] = final_status_for(new_status, interested=True)
        if decision == "shortlisted" and new_status == "SHORTLISTED":
            update["shortlisted_at"] = now
        await db[APPLICATIONS].update_one({"_id": application["_id"]}, {"$set": update})

        if new_status and new_status != old_status:
            await db[STATUS_HISTORY].insert_one({
                "application_id": application["_id"],
                "student_id": student["_id"],
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": old_status,
                "new_status": new_status,
                "reason": f"Company decision: {remark}" if remark else "Company decision imported",
                "notes": remark or None,
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "company_decision",
                "created_at": now,
            })
        preview.append(entry)

    if reconcile:
        counts["removed_from_shortlist"] = await _reconcile_shortlist(
            db, opportunity=opportunity, company=company,
            shortlisted_ids=shortlisted_ids, now=now, confirm=confirm,
        )

    if confirm and update_opportunity:
        await db[HIRING_OPPORTUNITIES].update_one(
            {"_id": opportunity["_id"]},
            {"$set": {
                "shortlist_imported_at": now,
                "shortlist_row_count": counts["rows"],
                "shortlists_count": len(shortlisted_ids),
                "shortlist_sync.shortlisted_student_ids": list(shortlisted_ids),
                "shortlist_sync.source_record_ids": sorted(source_record_ids),
            }},
        )
        await refresh_opportunity_counts(opportunity["_id"])

    return serialize_mongo({
        "mode": "applied" if confirm else "preview",
        "company": company.get("name"),
        "role": opportunity.get("role"),
        "has_remarks": bool(remark_header),
        "counts": counts,
        "rows": preview,
    })


async def import_shortlist(
    *, opportunity_id: str, raw_text: str, confirm: bool = False,
    reconcile: bool = True, update_opportunity: bool = True,
) -> dict:
    # A modern company sheet has a proper header row and a per-candidate remarks
    # column; route those to the decision-based importer. Old positional sheets
    # (no reliable header) fall through to the legacy path below.
    header_rows = read_response_rows(raw_text)
    if header_rows:
        field_map = build_field_map(list(header_rows[0].keys()))
        if field_map["uid"] or field_map["email"] or field_map["phone"] or field_map["name"]:
            return await _import_company_decisions(
                opportunity_id=opportunity_id, rows=header_rows, field_map=field_map, confirm=confirm,
                reconcile=reconcile, update_opportunity=update_opportunity,
            )

    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    rows = read_shortlist_rows(raw_text)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No rows found. Paste the shortlist sheet contents.",
        )

    now = datetime.now(timezone.utc)
    preview: list[dict[str, Any]] = []
    # A shortlist import ONLY marks people who already applied to this opening.
    # It never creates a student, and never creates an application: a shortlist
    # sheet carries no form data, so an application built from it would be an
    # empty shell. A shortlisted name with no application here means the
    # response sheet is missing - a gap to fix at the source, not to paper over.
    counts = {
        "rows": len(rows),
        "students_matched": 0,
        "matched_by_name": 0,
        "applications_to_mark": 0,
        "ambiguous": 0,
        "unmatched": 0,
        "removed_from_shortlist": 0,
    }
    willing = {"interested": 0, "not_interested": 0, "no_response": 0}
    shortlisted_ids: set = set()
    source_record_ids: set[str] = set()

    applicants = await build_applicant_index(db, opportunity["_id"])

    for index, cells in enumerate(rows, start=1):
        data = extract_shortlist_row(cells)
        if data.get("uid"):
            source_record_ids.add(data["uid"])
        willing[data["willing_to_join"] or "no_response"] += 1
        entry: dict[str, Any] = {
            "row": index,
            "name": data["name"],
            "email": data["email"],
            "phone": data["phone"],
            "willing_to_join": data["willing_to_join"],
        }

        # Matched only against people who applied to THIS opening.
        student, ambiguous = match_applicant(data, applicants)
        if ambiguous:
            entry["action"] = "skip"
            entry["reason"] = f"Several applicants match the name '{data['name']}'. Add an email to the sheet."
            counts["ambiguous"] += 1
            preview.append(entry)
            continue

        if not student:
            entry["action"] = "skip"
            entry["reason"] = (
                "Nobody with this name applied to this opening, so there is nothing to mark. "
                "Import their response sheet first if they did apply."
            )
            counts["unmatched"] += 1
            preview.append(entry)
            continue

        entry["matched_via"] = "name" if not (data.get("email") or data.get("phone") or data.get("uid")) else "id"
        if entry["matched_via"] == "name":
            counts["matched_by_name"] += 1
        counts["students_matched"] += 1

        existing_application = await db[APPLICATIONS].find_one(
            {"opportunity_id": opportunity["_id"], "student_id": student["_id"]}
        )
        if not existing_application:
            # Should not happen - applicants came from applications on this
            # opening - but never invent one if it somehow does.
            entry["action"] = "skip"
            entry["reason"] = "No application on this opening for this student."
            counts["unmatched"] += 1
            preview.append(entry)
            continue

        entry["action"] = "mark_shortlisted"
        entry["current_status"] = existing_application.get("current_status")
        counts["applications_to_mark"] += 1
        shortlisted_ids.add(student["_id"])

        if not confirm:
            preview.append(entry)
            continue

        # ---- write ----
        student_id = student["_id"]

        shortlist_sub = {
            "is_shortlisted": True,
            "resume": data["resume"],
            "call_date": data["call_date"],
            "call_status": data["call_status"],
            "willing_to_join": data["willing_to_join"],
            "willing_notes": data["willing_notes"],
            "source": "shortlist_paste",
            "imported_at": now,
        }

        old_status = status_for_api(existing_application)
        # Being on the shortlist does not un-do a later interview or offer.
        new_status = _keep_status(existing_application, "SHORTLISTED")
        if existing_application.get("current_status") == "SHORTLISTED":
            new_status = "SHORTLISTED"
        await db[APPLICATIONS].update_one(
            {"_id": existing_application["_id"]},
            {"$set": {
                "current_status": new_status,
                "final_status": final_status_for(new_status, interested=True),
                "shortlisted_at": now,
                "shortlist": shortlist_sub,
                "application_details.interested": True,
                "updated_at": now,
            }},
        )
        application_id = existing_application["_id"]

        if old_status != new_status:
            await db[STATUS_HISTORY].insert_one({
                "application_id": application_id,
                "student_id": student_id,
                "company_id": company["_id"],
                "opportunity_id": opportunity["_id"],
                "old_status": old_status,
                "new_status": new_status,
                "reason": "Marked shortlisted from pasted shortlist sheet",
                "notes": data["willing_notes"],
                "changed_by": None,
                "changed_by_role": "admin",
                "source": "shortlist_paste",
                "created_at": now,
            })
        preview.append(entry)

    if reconcile:
        counts["removed_from_shortlist"] = await _reconcile_shortlist(
            db, opportunity=opportunity, company=company,
            shortlisted_ids=shortlisted_ids, now=now, confirm=confirm,
        )

    if confirm and update_opportunity:
        await db[HIRING_OPPORTUNITIES].update_one(
            {"_id": opportunity["_id"]},
            {"$set": {
                "shortlist_imported_at": now,
                "shortlist_row_count": counts["rows"],
                "shortlists_count": len(shortlisted_ids),
                "shortlist_sync.shortlisted_student_ids": list(shortlisted_ids),
                "shortlist_sync.source_record_ids": sorted(source_record_ids),
            }},
        )
        await refresh_opportunity_counts(opportunity["_id"])

    if confirm:
        await refresh_opportunity_counts(opportunity["_id"])

    return serialize_mongo({
        "mode": "applied" if confirm else "preview",
        "company": company.get("name"),
        "role": opportunity.get("role"),
        "counts": counts,
        "willing_breakdown": willing,
        "rows": preview,
    })


async def sync_shortlist_sheet_incremental(*, opportunity_id: str) -> dict:
    """Import only previously unseen UUID-backed shortlist records.

    Shortlist sheets have no universal submission timestamp. UUID is the only
    stable source marker found in the supported exports. Full sync remains the
    reconciliation path for edits, deletions, reordering, and legacy sheets.
    """
    db = get_database()
    opportunity, company = await load_opportunity(db, opportunity_id)
    url = (opportunity.get("company_sheet") or "").strip()
    if not url:
        return serialize_mongo({
            "mode": "skipped",
            "opportunity_id": opportunity_id,
            "rows_scanned": 0,
            "rows_processed": 0,
            "message": "No shortlist sheet URL is stored for this opportunity.",
        })

    response_sync = opportunity.get("shortlist_sync") or {}
    if (
        "source_record_ids" not in response_sync
        or "shortlisted_student_ids" not in response_sync
        or link_changed_since_import(opportunity, "shortlist")
    ):
        # No checkpoint yet, or the link now points at a different sheet: run the
        # normal full import, which records the checkpoint.
        return await sync_from_sheet(opportunity_id=opportunity_id, kind="shortlist", confirm=True, force=True)

    raw_text = await fetch_sheet_text(url)
    header_rows = read_response_rows(raw_text)
    field_map = build_field_map(list(header_rows[0].keys())) if header_rows else {}
    is_header_source = bool(header_rows and (field_map.get("uid") or field_map.get("email") or field_map.get("phone") or field_map.get("name")))
    if is_header_source:
        rows = header_rows
        source_ids = [pick(row, field_map["uid"] or "") for row in rows]
    else:
        rows = read_shortlist_rows(raw_text)
        source_ids = [extract_shortlist_row(row).get("uid") for row in rows]
    if any(not value or not UUID_RE.fullmatch(value) for value in source_ids):
        # Without a stable UUID on every row, new rows can't be told from old
        # ones - re-import the sheet in full instead (it reconciles, so it is safe).
        return await import_fetched_sheet(db, opportunity, "shortlist", raw_text, url, confirm=True)

    previous_record_ids = set(response_sync.get("source_record_ids") or [])
    previous_student_ids = set(response_sync.get("shortlisted_student_ids") or [])
    unseen_indexes = [index for index, source_id in enumerate(source_ids) if source_id not in previous_record_ids]
    if not unseen_indexes:
        return serialize_mongo({
            "mode": "incremental",
            "opportunity_id": opportunity_id,
            "rows_scanned": len(rows),
            "rows_processed": 0,
            "shortlists_count": len(previous_student_ids),
            "message": "No new shortlist rows were found after the saved checkpoint.",
        })

    applicants = await build_applicant_index(db, opportunity["_id"])
    new_shortlisted_ids: set = set()
    for index in unseen_indexes:
        if is_header_source:
            identity = extract_identity(rows[index], field_map)
            remark_header = detect_remark_header(list(rows[index].keys()))
            decision, _ = classify_remark((rows[index].get(remark_header) or "").strip() if remark_header else "")
            is_shortlisted = decision in {"none", "shortlisted"}
        else:
            identity = extract_shortlist_row(rows[index])
            is_shortlisted = True
        student, ambiguous = match_applicant(identity, applicants)
        if is_shortlisted and not ambiguous and student:
            new_shortlisted_ids.add(student["_id"])

    if is_header_source:
        incremental_text = _rebuild_raw_text(list(rows[0].keys()), [rows[index] for index in unseen_indexes])
    else:
        incremental_text = "".join("\t".join(cell or "" for cell in rows[index]) + "\n" for index in unseen_indexes)
    result = await import_shortlist(
        opportunity_id=opportunity_id,
        raw_text=incremental_text,
        confirm=True,
        reconcile=False,
        update_opportunity=False,
    )

    now = datetime.now(timezone.utc)
    next_student_ids = previous_student_ids | new_shortlisted_ids
    await db[HIRING_OPPORTUNITIES].update_one(
        {"_id": opportunity["_id"]},
        {"$set": {
            "shortlist_sync": {
                **response_sync,
                "shortlisted_student_ids": list(next_student_ids),
                "source_record_ids": sorted(previous_record_ids | {
                    source_ids[index] for index in unseen_indexes
                }),
                "last_processed_source_id": source_ids[unseen_indexes[-1]],
                "last_successful_sync_at": now,
            },
            "shortlist_imported_at": now,
            "shortlist_row_count": len(rows),
            "shortlists_count": len(next_student_ids),
        }},
    )
    await refresh_opportunity_counts(opportunity["_id"])
    return serialize_mongo({
        "mode": "incremental",
        "opportunity_id": opportunity_id,
        "rows_scanned": len(rows),
        "rows_processed": len(unseen_indexes),
        "shortlists_count": len(next_student_ids),
        "last_processed_source_id": source_ids[unseen_indexes[-1]],
        "source_url": url,
        "result": result,
    })


# --------------------------------------------------------------------------
# master tracker - creates companies and their openings
#
# One row per opening (a company appears on several rows), so "add a company"
# is really "add one or more openings". Mirrors import_company_master.py; the
# CLI should eventually delegate here so the two cannot drift.
# --------------------------------------------------------------------------


def company_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")


# A real company name is never schedule/duration text. These catch the shifted
# rows a master sheet occasionally produces without touching real names (tested
# against all 274 rows: flags only the junk, passes ITMTB, CMB Greens LLP, ...).
NON_COMPANY_PATTERNS = (
    r"\bdays?\s+a\s+week\b",
    r"^\s*\d{1,2}\s*[-–:]\s*\d{1,2}\s*(am|pm)?\s*$",       # bare time range "9-6"
    r"^\s*\d+\s*months?\s*$",                                # "6 Months"
    r"\b\d{1,2}\s*(am|pm)\s*[-–to]+\s*\d{1,2}\s*(am|pm)\b",  # "9am-6pm"
)


def looks_like_schedule(name: str | None) -> bool:
    low = (name or "").strip().lower()
    return any(re.search(pattern, low) for pattern in NON_COMPANY_PATTERNS)


def parse_master_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip())
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%b-%d-%Y", "%B-%d-%Y", "%d %B %Y", "%d %b %Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_master_time(value: str | None) -> time | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", value.strip().upper())
    for fmt in ("%I:%M %p", "%I %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(normalized, fmt).time()
        except ValueError:
            continue
    return None


def combine_date_time(date_value: str | None, time_value: str | None) -> datetime | None:
    parsed_date = parse_master_date(date_value)
    if not parsed_date:
        return None
    return datetime.combine(parsed_date.date(), parse_master_time(time_value) or time.min, tzinfo=timezone.utc)


def master_opportunity_fields(row: dict[str, str | None]) -> dict[str, Any]:
    """Every opportunity column from one master row (company/role handled apart)."""
    return {
        "crm_poc": pick(row, "CRM POC"),
        "student_side_status": pick(row, "Student Side Status"),
        "hubspot_link": pick(row, "Hubspot Link"),
        "student_response_sheet": pick(row, "Student Response Sheet"),
        "company_sheet": pick(row, "Company Sheet"),
        "positions": pick(row, "#Positions"),
        "profiles_requested": pick(row, "# Profile Requested"),
        "profiles_shared": pick(row, "# No .of Profiles shared"),
        "mapping_pool": pick(row, "#Mapping Pool"),
        "eligible_as_per_pref": pick(row, "# Eligible as per Pref"),
        "filled_form_count": pick(row, "# Filled Form"),
        "interested_count": pick(row, "# Interested"),
        "date_of_sharing_profiles": pick(row, "Date of Sharing Profiles"),
        # The CRM's own "# shortlists" note. Kept under its own name: the
        # shortlists_count the dashboard reads is COUNTED from the shortlist
        # sheet, and a master import must not overwrite it with this text (a
        # blank cell here used to blank the real count on every sync).
        "master_shortlists_count": pick(row, "# shortlists"),
        "company_status": pick(row, "Company Status"),
        "process_datetime": pick(row, "Date  & Time of Process", "Date & Time of Process"),
        "process_details": pick(row, "Company Process Details"),
        "screening_round": pick(row, "Screening Round/Telephonic Round"),
        "assignment_round": pick(row, "Assignement Round", "Assignment Round"),
        "tr_1": pick(row, "TR 1"),
        "next_process": pick(row, "Next Process"),
        "must_have_skills": pick(row, "Skills required (Must)"),
        "good_to_have_skills": pick(row, "Skills required (Good to Have)"),
        "stipend": pick(row, "Stipend"),
        "location": pick(row, "Location"),
        "duration": pick(row, "Duration"),
        "day_timings": pick(row, "Day & timings"),
        "company_feedback": pick(row, "Success Team_Company Feedback"),
        "scheduled_date": pick(row, "Scheduled Date"),
        "interview_process": pick(row, "Interview Process (e.g. TR, MR, Assessment)"),
        "action_items": pick(row, "Action Items"),
        "hiring_intelligence": pick(row, "Hiring Intelligence"),
        "rsa_notes": pick(row, "RSA"),
    }


# Every opening column the master sheet owns. Used to load an existing opening
# for the change diff, so a full sync can say which columns actually changed
# instead of reporting every row as "updated".
MASTER_COLUMN_FIELDS = tuple(master_opportunity_fields({}).keys())

# Written on every import but not part of "did this row change": bookkeeping,
# the raw row, and the link-change stamps (derived from the links themselves).
NON_COMPARED_FIELDS = frozenset({
    "updated_at", "raw_company_row", "source_sheet_row",
    "response_sheet_changed_at", "previous_student_response_sheet",
    "company_sheet_changed_at", "previous_company_sheet",
})


def is_unknown_role(role: str | None) -> bool:
    return not (role or "").strip() or (role or "").strip().lower() == "unknown"


def _as_utc(value: datetime | None) -> datetime | None:
    """Mongo hands dates back naive; treat them as the UTC they were stored in."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _comparable(value: Any) -> Any:
    """One value as the diff sees it: blank and missing are the same thing, and a
    naive stored date is the UTC it was written as."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, datetime):
        return _as_utc(value)
    return value


def master_row_changes(existing: dict | None, set_fields: dict[str, Any]) -> list[dict[str, Any]]:
    """The columns this master row actually changes on an existing opening.

    A full sync re-writes every column of every row, so without this an admin is
    told 400 openings were "updated" when only two cells moved. Bookkeeping
    fields are ignored - they change on every import by definition.
    """
    if existing is None:
        return []
    changes = []
    for field, new_value in set_fields.items():
        if field in NON_COMPARED_FIELDS:
            continue
        old_value = _comparable(existing.get(field))
        if old_value != _comparable(new_value):
            changes.append({"field": field, "old": old_value, "new": _comparable(new_value)})
    return changes


class MasterIndex:
    """Existing companies and openings for one master import, held in memory.

    Loaded with two queries up front instead of several per row (per-row lookups
    made a full-sheet import take minutes), then kept current as rows are planned
    so a later row sees what an earlier row in the same import created or
    upgraded. Openings are keyed by company_key, which a new company has before
    it has an _id.
    """

    def __init__(self, company_keys_by_id: dict, opportunities: list[dict]):
        self.companies: set[str] = set(company_keys_by_id.values())
        self.exact: dict[tuple, dict] = {}
        self.unknown: dict[str, list[dict]] = {}
        for doc in opportunities:
            ckey = company_keys_by_id.get(doc.get("company_id"))
            if ckey:
                self._add(ckey, doc)

    def _add(self, ckey: str, doc: dict) -> None:
        self.exact[(ckey, doc.get("role_key"), doc.get("opportunity_key"))] = doc
        if is_unknown_role(doc.get("role_key")):
            self.unknown.setdefault(ckey, []).append(doc)

    def has_company(self, ckey: str) -> bool:
        return ckey in self.companies

    def find(self, ckey: str, role_key: str, opportunity_key: str, received_at: datetime | None) -> dict | None:
        """The exact opening, or an unknown-role opening on the same date to upgrade."""
        exact = self.exact.get((ckey, role_key, opportunity_key))
        if exact or not received_at or is_unknown_role(role_key):
            return exact
        day = received_at.date()
        for doc in self.unknown.get(ckey, []):
            stored = _as_utc(doc.get("opportunity_received_at"))
            if stored and stored.date() == day:
                return doc
        return None

    def record(self, ckey: str, existing: dict | None, fields: dict) -> None:
        """Reflect a planned write so later rows in the same import see it."""
        self.companies.add(ckey)
        doc = existing if existing is not None else {}
        if existing is not None:
            self.exact.pop((ckey, existing.get("role_key"), existing.get("opportunity_key")), None)
            if ckey in self.unknown:
                self.unknown[ckey] = [item for item in self.unknown[ckey] if item is not existing]
        doc.update(fields)
        self._add(ckey, doc)


async def load_master_index(db, company_keys: set[str]) -> MasterIndex:
    if not company_keys:
        return MasterIndex({}, [])
    companies = await db[COMPANIES].find(
        {"company_key": {"$in": sorted(company_keys)}}, {"_id": 1, "company_key": 1}
    ).to_list(length=None)
    keys_by_id = {company["_id"]: company["company_key"] for company in companies}
    # Archived openings are loaded too (no deleted_at filter): the sheet is the
    # source of truth, so a row that comes back must restore the opening it was
    # deleted from - with its applications - instead of creating a second one.
    projection = {
        "_id": 1, "company_id": 1, "role": 1, "role_key": 1, "opportunity_key": 1,
        "opportunity_received_at": 1, "opportunity_received_on": 1, "received_time": 1,
        "company_name": 1, "deleted_at": 1,
        **{field: 1 for field in MASTER_COLUMN_FIELDS},
    }
    opportunities = await db[HIRING_OPPORTUNITIES].find(
        {"company_id": {"$in": list(keys_by_id)}}, projection,
    ).to_list(length=None) if keys_by_id else []
    return MasterIndex(keys_by_id, opportunities)


async def recent_openings_for_refresh(*, days: int, exclude_ids: set[str] | None = None) -> list[dict]:
    """Live openings received in the last `days`, as the sync pipeline's opening
    records.

    Pull only new creates the openings the Master sheet just gained, but the ones
    it created yesterday keep receiving applicants. These are the openings it
    refreshes alongside, so no one has to press Force on an opening by hand.
    """
    db = get_database()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    excluded = exclude_ids or set()
    openings = await db[HIRING_OPPORTUNITIES].find(
        {"deleted_at": {"$exists": False}, "opportunity_received_at": {"$gte": cutoff}},
        {
            "_id": 1, "company_id": 1, "company_name": 1, "role": 1,
            "opportunity_received_on": 1, "student_response_sheet": 1, "company_sheet": 1,
        },
    ).sort("opportunity_received_at", -1).to_list(length=None)
    openings = [doc for doc in openings if str(doc["_id"]) not in excluded]
    if not openings:
        return []

    names = {
        company["_id"]: company.get("name")
        for company in await db[COMPANIES].find(
            {"_id": {"$in": list({doc.get("company_id") for doc in openings})}}, {"_id": 1, "name": 1}
        ).to_list(length=None)
    }
    return [
        {
            "opportunity_id": str(doc["_id"]),
            "is_new": False,
            "restored": False,
            "refresh": True,
            "master": {"status": "unchanged"},
            "company": names.get(doc.get("company_id")) or doc.get("company_name"),
            "role": doc.get("role"),
            "received_on": doc.get("opportunity_received_on"),
            "response_url_present": bool((doc.get("student_response_sheet") or "").strip()),
            "shortlist_url_present": bool((doc.get("company_sheet") or "").strip()),
        }
        for doc in openings
    ]


async def _apply_master_plan(db, planned: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Write the planned rows with batched round trips (not several per row) and
    return the processed openings, in sheet order, with their ids.

    A row that changes nothing is still returned - its responses and shortlist
    are synced like any other - but nothing is written for it, so a re-sync of an
    unchanged sheet leaves the openings and companies exactly as they were.
    """
    writable = [item for item in planned if item["write"]]
    if writable:
        await db[COMPANIES].bulk_write([
            UpdateOne(
                {"company_key": item["ckey"]},
                {
                    "$set": {"name": item["name"], "company_key": item["ckey"], "updated_at": now},
                    "$setOnInsert": {"created_at": now},
                    "$addToSet": {"aliases": item["name"], "sources": "company_master_paste"},
                },
                upsert=True,
            )
            for item in writable
        ], ordered=True)
    company_ids = {
        company["company_key"]: company["_id"]
        for company in await db[COMPANIES].find(
            {"company_key": {"$in": sorted({item["ckey"] for item in planned})}}, {"_id": 1, "company_key": 1}
        ).to_list(length=None)
    }

    operations = []
    for item in writable:
        company_id = company_ids[item["ckey"]]
        fields = item["set_fields"]
        target = item["target"]
        if target is None:
            opportunity_filter = {"company_id": company_id, "role_key": fields["role_key"], "opportunity_key": fields["opportunity_key"]}
        elif "_id" in target:
            opportunity_filter = target
        else:
            opportunity_filter = {"company_id": company_id, **target}
        update: dict[str, Any] = {
            "$set": {"company_id": company_id, **fields},
            "$setOnInsert": {"source": "company_master_paste", "created_at": now},
        }
        if item["restored"]:
            # The sheet still carries this opening, so an earlier delete is undone
            # here - the opening and its applications come back as they were.
            update["$unset"] = {"deleted_at": "", "deleted_by": "", "deletion_reason": ""}
            update["$set"]["restored_at"] = now
        operations.append(UpdateOne(opportunity_filter, update, upsert=True))
    if operations:
        await db[HIRING_OPPORTUNITIES].bulk_write(operations, ordered=True)

    ids_by_identity = {
        (doc["company_id"], doc.get("role_key"), doc.get("opportunity_key")): doc["_id"]
        for doc in await db[HIRING_OPPORTUNITIES].find(
            {"company_id": {"$in": list(company_ids.values())}},
            {"_id": 1, "company_id": 1, "role_key": 1, "opportunity_key": 1},
        ).to_list(length=None)
    }
    processed = []
    for item in planned:
        fields = item["set_fields"]
        opportunity_id = ids_by_identity.get((company_ids[item["ckey"]], fields["role_key"], fields["opportunity_key"]))
        if opportunity_id is None:
            continue
        if item["is_new"]:
            master_status = "created"
        elif item["restored"]:
            master_status = "restored"
        elif item["write"]:
            master_status = "updated"
        else:
            master_status = "unchanged"
        processed.append({
            "opportunity_id": str(opportunity_id),
            "is_new": item["is_new"],
            "restored": item["restored"],
            "master": {"status": master_status, "changes": item["changes"]},
            **item["summary"],
        })
    return processed


async def import_master(
    *, raw_text: str, confirm: bool = False, collect_opportunity_ids: bool = False,
    since: datetime | None = None,
) -> dict:
    """Create companies and their openings from pasted master-tracker rows.

    A header row is required so columns can be matched by name. Both the company
    and each opening are upserted, so re-pasting the same rows updates rather
    than duplicating.

    With `since`, a row is only considered when it is not in the database yet
    (whatever its date or position in the sheet) or is dated on/after `since`;
    every other row is left out of the result entirely.
    """
    db = get_database()
    rows = read_response_rows(raw_text)  # header-based TSV/CSV, same parser
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No rows found. Paste the master sheet rows including the header row.",
        )

    now = datetime.now(timezone.utc)
    preview: list[dict[str, Any]] = []
    counts = {
        "rows": 0,
        "companies_new": 0,
        "companies_existing": 0,
        "opportunities_to_create": 0,
        "opportunities_to_update": 0,
        "opportunities_to_restore": 0,
        "opportunities_unchanged": 0,
        "response_links_changed": 0,
        "company_links_changed": 0,
        "skipped": 0,
    }
    seen_companies: set[str] = set()
    planned: list[dict[str, Any]] = []
    index = await load_master_index(
        db, {company_key(name) for name in (pick(row, "Company Name") for row in rows) if name}
    )

    for row_number, row in enumerate(rows, start=1):
        name = pick(row, "Company Name")
        role = pick(row, "Role") or "unknown"
        received_on = pick(row, "Opportunity Received On")
        received_time = pick(row, "Received Time")
        source_sheet_row = row_number + 1
        entry: dict[str, Any] = {"row": row_number, "source_sheet_row": source_sheet_row, "company": name, "role": role, "received_on": received_on}

        # A shifted/partial row in the master lands schedule or duration text in
        # the Company Name column ("5 days a week, 9-6", "6 Months"). Those are
        # skipped below so they don't become junk companies.
        valid = bool(name) and not looks_like_schedule(name)
        ckey = company_key(name)
        role_key = company_key(role)
        opportunity_received_at = combine_date_time(received_on, received_time)
        opportunity_key = company_key(
            opportunity_received_at.isoformat()
            if opportunity_received_at
            else f"{received_on or 'no-date'}-{received_time or 'no-time'}"
        )
        existing_opportunity = index.find(ckey, role_key, opportunity_key, opportunity_received_at) if valid else None
        archived = bool(existing_opportunity and existing_opportunity.get("deleted_at"))

        if since is not None:
            recent = bool(opportunity_received_at and opportunity_received_at >= since)
            # An archived opening is considered whatever its date: the sheet still
            # lists it, so this row is what brings it back.
            if not recent and not archived and not (valid and existing_opportunity is None):
                continue  # already imported and older than the checkpoint
        counts["rows"] += 1

        if not name:
            entry["action"] = "skip"
            entry["reason"] = "Row has no Company Name."
            counts["skipped"] += 1
            preview.append(entry)
            continue

        if not valid:
            entry["action"] = "skip"
            entry["reason"] = "Company Name looks like schedule/duration text - likely a shifted row in the sheet."
            entry["suspicious"] = True
            counts["skipped"] += 1
            preview.append(entry)
            continue

        is_new_company = not index.has_company(ckey)
        # Count a company once per paste even if it spans several rows.
        if ckey not in seen_companies:
            seen_companies.add(ckey)
            counts["companies_new" if is_new_company else "companies_existing"] += 1
        entry["company_new"] = is_new_company

        opp_fields = master_opportunity_fields(row)

        # Detect a changed response/shortlist sheet link so it can be re-pulled.
        # Only a real change of an existing, non-empty URL counts.
        change_stamps: dict[str, Any] = {}
        if existing_opportunity:
            for field, changed_at, previous, count_key in (
                ("student_response_sheet", "response_sheet_changed_at", "previous_student_response_sheet", "response_links_changed"),
                ("company_sheet", "company_sheet_changed_at", "previous_company_sheet", "company_links_changed"),
            ):
                old_url = (existing_opportunity.get(field) or "").strip()
                new_url = (opp_fields.get(field) or "").strip()
                if old_url and new_url and old_url != new_url:
                    change_stamps[changed_at] = now
                    change_stamps[previous] = old_url
                    counts[count_key] += 1
                    entry[count_key] = True

        # Where the write lands: the matched opening by _id, or - when an earlier
        # row in this same import planned it and it has no _id yet - by the
        # identity it has at this point.
        if existing_opportunity is None:
            target = None
        elif existing_opportunity.get("_id") is not None:
            target = {"_id": existing_opportunity["_id"]}
        else:
            target = {"role_key": existing_opportunity["role_key"], "opportunity_key": existing_opportunity["opportunity_key"]}
        set_fields = {
            "company_name": name,
            "role": role,
            "role_key": role_key,
            "opportunity_key": opportunity_key,
            "opportunity_received_on": received_on,
            "received_time": received_time,
            "opportunity_received_at": opportunity_received_at,
            **opp_fields,
            "raw_company_row": row,
            "source_sheet_row": source_sheet_row,
            "updated_at": now,
            **change_stamps,
        }

        # What this row actually does: create, restore a deleted opening, change
        # some columns, or nothing at all. Only the first three are written.
        changes = master_row_changes(existing_opportunity, set_fields)
        is_new = existing_opportunity is None
        if is_new:
            entry["action"] = "create_opportunity"
            counts["opportunities_to_create"] += 1
        elif archived:
            entry["action"] = "restore_opportunity"
            counts["opportunities_to_restore"] += 1
        elif changes:
            entry["action"] = "update_opportunity"
            counts["opportunities_to_update"] += 1
        else:
            entry["action"] = "unchanged"
            counts["opportunities_unchanged"] += 1
        if changes:
            entry["changes"] = changes

        index.record(ckey, existing_opportunity, {
            "role_key": role_key,
            "opportunity_key": opportunity_key,
            "opportunity_received_at": opportunity_received_at,
            "student_response_sheet": opp_fields.get("student_response_sheet"),
            "company_sheet": opp_fields.get("company_sheet"),
            # A restored opening is live again for any later row in this import.
            **({"deleted_at": None} if archived else {}),
        })
        preview.append(entry)

        if confirm:
            planned.append({
                "ckey": ckey,
                "name": name,
                "target": target,
                "is_new": is_new,
                "restored": archived,
                "changes": changes,
                "write": bool(is_new or archived or changes),
                "set_fields": set_fields,
                "summary": {
                    "company": name,
                    "role": role,
                    "received_on": received_on,
                    "response_url_present": bool(opp_fields.get("student_response_sheet")),
                    "shortlist_url_present": bool(opp_fields.get("company_sheet")),
                },
            })

    processed_opportunities = await _apply_master_plan(db, planned, now) if planned else []
    result = {"mode": "applied" if confirm else "preview", "counts": counts, "rows": preview}
    if collect_opportunity_ids or confirm:
        result["opportunity_ids"] = list(dict.fromkeys(item["opportunity_id"] for item in processed_opportunities))
        result["processed_opportunities"] = processed_opportunities
        result["opportunity_results"] = processed_opportunities
    return serialize_mongo(result)


async def import_master_from_url(*, url: str, confirm: bool = False) -> dict:
    """Fetch the master tracker sheet from its public URL and import it.

    Same as pasting, but the admin gives a link instead. Works only for
    'anyone with the link' sheets; a restricted one raises a clear message.
    """
    raw_text = await fetch_sheet_text(url)
    result = await import_master(raw_text=raw_text, confirm=confirm)
    result["source_url"] = url
    return result


async def with_master_header(raw_text: str, url: str | None) -> str:
    """Rows copied out of the Master sheet usually come without its header row;
    borrow the header from the Master sheet so columns still match by name."""
    text = (raw_text or "").lstrip("\r\n")
    first_record = next(csv.reader(io.StringIO(text, newline=""), delimiter=_sniff_delimiter(text[:2000])), [])
    if any(normalize_header(cell or "") == "company_name" for cell in first_record):
        return raw_text
    master_url = (url or get_settings().student_sheet_url or "").strip()
    if not master_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "These rows have no header row. Copy the header row as well, or enter the "
                "Master sheet link so its header can be used."
            ),
        )
    master_text = await fetch_sheet_text(master_url)
    header = next(csv.reader(io.StringIO(master_text, newline=""), delimiter="\t"), [])
    return "\t".join(re.sub(r"\s+", " ", cell) for cell in header) + "\n" + text


async def import_master_paste(*, raw_text: str, url: str | None = None, confirm: bool = False) -> dict:
    """Pasted Master rows, with or without the header row."""
    return await import_master(raw_text=await with_master_header(raw_text, url), confirm=confirm)


async def import_master_incremental_from_url(*, url: str) -> dict:
    """Import Master rows that are new, plus rows dated on/after the newest stored opening.

    The whole sheet is fetched (one ~1s request) and matched against the database
    by opening identity, so a new row is picked up wherever it sits in the sheet
    and whatever date it carries - no row-position watermark that can drift. Rows
    on/after the checkpoint date are re-applied too, so the latest openings keep
    refreshing their response and shortlist data.
    """
    db = get_database()
    latest = await db[HIRING_OPPORTUNITIES].find(
        {"opportunity_received_at": {"$type": "date"}},
        {"opportunity_received_at": 1},
    ).sort("opportunity_received_at", -1).limit(1).to_list(length=1)
    if not latest or latest[0].get("opportunity_received_at") is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Incremental sync requires an existing synchronized Master dataset. Run Fetch entire sheet data first.",
        )

    latest_date = _as_utc(latest[0]["opportunity_received_at"])
    raw_text = await fetch_sheet_text(url)
    rows_scanned = len(read_response_rows(raw_text))
    result = (
        await import_master(raw_text=raw_text, confirm=True, collect_opportunity_ids=True, since=latest_date)
        if rows_scanned
        else {}
    )
    counts = result.get("counts", {})
    summary = {
        "mode": "incremental",
        "rows_scanned": rows_scanned,
        "rows_processed": (
            counts.get("opportunities_to_create", 0)
            + counts.get("opportunities_to_update", 0)
            + counts.get("opportunities_to_restore", 0)
        ),
        "opportunities_created": counts.get("opportunities_to_create", 0),
        "opportunities_updated": counts.get("opportunities_to_update", 0),
        "opportunities_restored": counts.get("opportunities_to_restore", 0),
        "companies_created": counts.get("companies_new", 0),
        "companies_updated": counts.get("companies_existing", 0),
        "rows_skipped": counts.get("skipped", 0),
        "errors": [],
        "latest_opportunity_date": latest_date.isoformat(),
        "opportunity_ids": result.get("opportunity_ids", []),
        "processed_opportunities": result.get("processed_opportunities", []),
    }
    if not summary["rows_processed"]:
        summary["message"] = "No new opportunities found in the Master sheet."
    return summary
