from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.db.collections import COMPANIES, HIRING_OPPORTUNITIES
from app.services import sheet_import_service

URL = "https://docs.google.com/spreadsheets/d/id/edit"
HEADER = "Opportunity Received On\tReceived Time\tCompany Name\tRole\tStudent Response Sheet\tCompany Sheet\n"


def sheet(*rows):
    return HEADER + "".join("\t".join(row + ("",) * (6 - len(row))) + "\n" for row in rows)


@pytest.mark.parametrize("url", [
    "https://docs.google.com/spreadsheets/d/id/edit",
    "https://docs.google.com/spreadsheets/d/id/edit?gid=1914832966",
    "https://docs.google.com/spreadsheets/d/id/edit?gid=1914832966#gid=1914832966",
])
def test_valid_google_sheet_url_forms_are_accepted(url):
    assert sheet_import_service.sheet_export_url(url).startswith("https://docs.google.com/spreadsheets/d/id/export")


# ---- in-memory Mongo: just enough of find / bulk_write for the master import ----

def _matches(doc, query):
    for key, expected in query.items():
        value = doc.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and value not in expected["$in"]:
                return False
            if "$type" in expected and not isinstance(value, datetime):
                return False
        elif value != expected:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, field, direction):
        self.docs = sorted(self.docs, key=lambda doc: doc[field], reverse=direction == -1)
        return self

    def limit(self, count):
        self.docs = self.docs[:count]
        return self

    async def to_list(self, length=None):
        return [dict(doc) for doc in self.docs]


class Collection:
    def __init__(self, db):
        self.db = db
        self.docs = []

    def find(self, query, projection=None):
        self.db.calls += 1
        return Cursor([doc for doc in self.docs if _matches(doc, query)])

    async def bulk_write(self, operations, ordered=True):
        self.db.calls += 1
        for operation in operations:
            update = operation._doc
            doc = next((item for item in self.docs if _matches(item, operation._filter)), None)
            if doc is None:
                if not operation._upsert:
                    continue
                doc = {"_id": ObjectId(), **operation._filter, **update.get("$setOnInsert", {})}
                self.docs.append(doc)
            doc.update(update.get("$set", {}))
            for field in update.get("$unset", {}):
                doc.pop(field, None)
            for field, value in update.get("$addToSet", {}).items():
                values = doc.setdefault(field, [])
                if value not in values:
                    values.append(value)


class Database:
    def __init__(self):
        self.calls = 0
        self.collections = {COMPANIES: Collection(self), HIRING_OPPORTUNITIES: Collection(self)}

    def __getitem__(self, name):
        return self.collections[name]

    def opportunities(self):
        return self.collections[HIRING_OPPORTUNITIES].docs

    def seed(self, name, role, received_at, **extra):
        """Store an opening the way Mongo returns it: with a naive datetime."""
        key = sheet_import_service.company_key
        company = next((c for c in self[COMPANIES].docs if c["company_key"] == key(name)), None)
        if company is None:
            company = {"_id": ObjectId(), "company_key": key(name), "name": name}
            self[COMPANIES].docs.append(company)
        doc = {
            "_id": ObjectId(),
            "company_id": company["_id"],
            "company_name": name,
            "role": role,
            "role_key": key(role),
            "opportunity_key": key(received_at.replace(tzinfo=timezone.utc).isoformat()),
            "opportunity_received_at": received_at,
            **extra,
        }
        self[HIRING_OPPORTUNITIES].docs.append(doc)
        return doc


@pytest.fixture
def db(monkeypatch):
    database = Database()
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: database)
    return database


# ---- full import ----

@pytest.mark.asyncio
async def test_full_import_does_not_query_per_row(db):
    rows = [("1-Sep-2026", "10:00 AM", f"Company {n}", "AI Intern") for n in range(60)]

    result = await sheet_import_service.import_master(raw_text=sheet(*rows), confirm=True)

    # Load companies + write companies + re-read companies + write openings + read ids.
    assert db.calls <= 6
    assert result["counts"]["opportunities_to_create"] == 60
    assert len(db.opportunities()) == 60
    assert len(result["opportunity_ids"]) == 60
    assert db.opportunities()[0]["source_sheet_row"] == 2


@pytest.mark.asyncio
async def test_preview_uses_two_queries_and_writes_nothing(db):
    db.seed("Acme", "AI Intern", datetime(2026, 9, 1, 10))
    before = [dict(doc) for doc in db.opportunities()]

    result = await sheet_import_service.import_master(
        raw_text=sheet(("1-Sep-2026", "10:00 AM", "Acme", "AI Intern"), ("2-Sep-2026", "", "Globex", "SDE")),
    )

    assert db.calls == 2
    assert db.opportunities() == before
    assert result["counts"]["opportunities_to_update"] == 1
    assert result["counts"]["opportunities_to_create"] == 1


@pytest.mark.asyncio
async def test_reimport_updates_instead_of_duplicating(db):
    text = sheet(("1-Sep-2026", "10:00 AM", "Acme", "AI Intern"), ("2-Sep-2026", "", "Globex", "SDE"))

    first = await sheet_import_service.import_master(raw_text=text, confirm=True)
    second = await sheet_import_service.import_master(raw_text=text, confirm=True)

    assert len(db.opportunities()) == 2
    assert second["counts"]["opportunities_to_create"] == 0
    # Nothing in the sheet moved, so nothing is written - but both openings are
    # still returned, so their responses and shortlists are synced as usual.
    assert second["counts"]["opportunities_to_update"] == 0
    assert second["counts"]["opportunities_unchanged"] == 2
    assert second["opportunity_ids"] == first["opportunity_ids"]
    assert [item["master"]["status"] for item in second["processed_opportunities"]] == ["unchanged", "unchanged"]


@pytest.mark.asyncio
async def test_duplicate_rows_in_one_import_create_once_then_update(db):
    row = ("1-Sep-2026", "10:00 AM", "Acme", "AI Intern")

    result = await sheet_import_service.import_master(raw_text=sheet(row, row), confirm=True)

    assert len(db.opportunities()) == 1
    assert result["counts"]["opportunities_to_create"] == 1
    assert result["counts"]["opportunities_to_update"] == 1
    assert [item["is_new"] for item in result["processed_opportunities"]] == [True, False]


@pytest.mark.asyncio
async def test_real_role_upgrades_unknown_role_opening_on_same_day(db):
    existing = db.seed("Acme", "unknown", datetime(2026, 8, 20, 9))

    result = await sheet_import_service.import_master(
        raw_text=sheet(("20-Aug-2026", "12:00 PM", "Acme", "Flutter Intern")), confirm=True,
    )

    assert result["counts"]["opportunities_to_update"] == 1
    assert len(db.opportunities()) == 1
    assert existing["role_key"] == "flutter-intern"


@pytest.mark.asyncio
async def test_changed_columns_are_listed_and_unchanged_rows_are_not_written(db):
    """A full sync must say which columns moved, not call every row an update."""
    existing = db.seed(
        "Acme", "AI Intern", datetime(2026, 9, 1, 10),
        opportunity_received_on="1-Sep-2026", received_time="10:00 AM",
        stipend="20000", location="Remote",
    )
    existing["updated_at"] = "untouched"

    result = await sheet_import_service.import_master(
        raw_text=HEADER.rstrip("\n") + "\tStipend\tLocation\n"
        + "1-Sep-2026\t10:00 AM\tAcme\tAI Intern\t\t\t25000\tRemote\n",
        confirm=True,
    )

    row = result["rows"][0]
    assert row["action"] == "update_opportunity"
    assert row["changes"] == [{"field": "stipend", "old": "20000", "new": "25000"}]
    assert result["counts"]["opportunities_to_update"] == 1
    assert result["counts"]["opportunities_unchanged"] == 0
    assert existing["stipend"] == "25000"
    assert existing["updated_at"] != "untouched"


@pytest.mark.asyncio
async def test_deleted_opening_comes_back_when_its_row_is_still_in_the_sheet(db):
    """Re-adding a deleted opening restores it - with its applications - rather
    than being silently skipped or duplicated."""
    deleted = db.seed(
        "Rajlaxmi Solutions", "SDE Intern", datetime(2026, 9, 8, 22, 45),
        deleted_at=datetime(2026, 9, 12, 12, 53), deleted_by={"email": "admin@example.com"},
        deletion_reason="for testing",
    )

    result = await sheet_import_service.import_master(
        raw_text=sheet(("8-Sep-2026", "10:45 PM", "Rajlaxmi Solutions", "SDE Intern")), confirm=True,
    )

    assert len(db.opportunities()) == 1  # restored in place, not duplicated
    assert result["counts"]["opportunities_to_restore"] == 1
    assert result["rows"][0]["action"] == "restore_opportunity"
    assert result["processed_opportunities"][0]["master"]["status"] == "restored"
    assert result["processed_opportunities"][0]["restored"] is True
    assert "deleted_at" not in deleted and "deletion_reason" not in deleted
    assert deleted["restored_at"] is not None


@pytest.mark.asyncio
async def test_incremental_restores_a_deleted_opening_older_than_the_checkpoint(db, monkeypatch):
    """The checkpoint must not hide a deleted row: it is older than the newest
    opening, but it is the row that brings the opening back."""
    deleted = db.seed(
        "Rajlaxmi Solutions", "SDE Intern", datetime(2026, 9, 8, 22, 45),
        deleted_at=datetime(2026, 9, 12, 12, 53),
    )
    db.seed("Newer Co", "AI Intern", datetime(2026, 9, 15, 14, 29))
    text = sheet(
        ("8-Sep-2026", "10:45 PM", "Rajlaxmi Solutions", "SDE Intern"),
        ("15-Sep-2026", "2:29 PM", "Newer Co", "AI Intern"),
    )

    async def fetch(url):
        return text

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    result = await sheet_import_service.import_master_incremental_from_url(url=URL)

    assert result["opportunities_restored"] == 1
    assert "deleted_at" not in deleted
    restored = [item for item in result["processed_opportunities"] if item["restored"]]
    assert [item["company"] for item in restored] == ["Rajlaxmi Solutions"]


@pytest.mark.asyncio
async def test_changed_response_link_is_stamped(db):
    existing = db.seed("Acme", "AI Intern", datetime(2026, 9, 1, 10), student_response_sheet="https://old")

    result = await sheet_import_service.import_master(
        raw_text=sheet(("1-Sep-2026", "10:00 AM", "Acme", "AI Intern", "https://new")), confirm=True,
    )

    assert result["counts"]["response_links_changed"] == 1
    assert existing["previous_student_response_sheet"] == "https://old"
    assert existing["student_response_sheet"] == "https://new"


# ---- pull only newly added ----

@pytest.mark.asyncio
async def test_incremental_picks_up_new_rows_whatever_their_date_or_position(db, monkeypatch):
    # The newest opening carries a stale row position (row 2) - the anchor that
    # made the old windowed sync read February rows and import nothing.
    db.seed("Old Co", "SDE", datetime(2026, 2, 4))
    db.seed("Verona Matchmaking", "Mobile Intern", datetime(2026, 9, 7, 12, 50), source_sheet_row=387)
    db.seed("Blue Machines AI", "FDE Intern", datetime(2026, 9, 11, 15, 18), source_sheet_row=2)
    text = sheet(
        ("4-Feb-2026", "", "Old Co", "SDE"),
        ("7-Sep-2026", "12:50 PM", "Verona Matchmaking", "Mobile Intern"),
        ("8-Sep-2026", "10:45 PM", "Rajlaxmi Solutions", "SDE Intern"),
        ("11-Sep-2026", "15:18", "Blue Machines AI", "FDE Intern"),
        ("9-Sep-2026", "2:02 PM", "Freight Tiger", "SDE intern"),
    )

    async def fetch(url):
        return text

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    result = await sheet_import_service.import_master_incremental_from_url(url=URL)

    processed = [(item["company"], item["is_new"]) for item in result["processed_opportunities"]]
    assert processed == [("Rajlaxmi Solutions", True), ("Blue Machines AI", False), ("Freight Tiger", True)]
    assert result["rows_scanned"] == 5
    assert result["opportunities_created"] == 2
    assert result["opportunities_updated"] == 1
    assert len(db.opportunities()) == 5


@pytest.mark.asyncio
async def test_incremental_with_nothing_new_is_a_successful_noop(db, monkeypatch):
    db.seed("Acme", "AI Intern", datetime(2026, 9, 1, 10))
    db.seed("Globex", "SDE", datetime(2026, 9, 5, 9))
    text = sheet(
        ("", "", "", "Orphan role"),                      # old junk rows stay out of scope
        ("", "", "5 days a week, 9-6", ""),
        ("1-Sep-2026", "10:00 AM", "Acme", "AI Intern"),
    )

    async def fetch(url):
        return text

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    result = await sheet_import_service.import_master_incremental_from_url(url=URL)

    assert result["rows_processed"] == 0
    assert result["rows_skipped"] == 0
    assert "No new opportunities found" in result["message"]


@pytest.mark.asyncio
async def test_incremental_header_only_sheet_is_a_noop(db, monkeypatch):
    db.seed("Acme", "AI Intern", datetime(2026, 9, 1, 10))

    async def fetch(url):
        return HEADER

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    result = await sheet_import_service.import_master_incremental_from_url(url=URL)

    assert result["mode"] == "incremental"
    assert result["rows_scanned"] == 0
    assert result["rows_processed"] == 0
    assert "No new opportunities found" in result["message"]


@pytest.mark.asyncio
async def test_incremental_requires_full_sync_first(db):
    with pytest.raises(HTTPException) as error:
        await sheet_import_service.import_master_incremental_from_url(url=URL)

    assert error.value.status_code == 409
    assert "Fetch entire sheet data first" in str(error.value.detail)
