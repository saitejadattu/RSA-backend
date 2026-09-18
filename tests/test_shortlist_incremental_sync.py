import pytest

from app.services import sheet_import_service

URL = "https://docs.google.com/spreadsheets/d/abc/edit"


class FakeCollection:
    def __init__(self):
        self.updated = []

    async def update_one(self, *args, **kwargs):
        self.updated.append((args, kwargs))
        update = kwargs.get("update", args[1] if len(args) > 1 else {})
        self.document = {**getattr(self, "document", {}), **update.get("$set", {})}
        return type("Result", (), {"matched_count": 1})()


class FakeDB:
    def __init__(self):
        self.hiring_opportunities = FakeCollection()

    def __getitem__(self, name):
        assert name == "hiring_opportunities"
        return self.hiring_opportunities


SHEET = (
    "UUID\tFull Name\tFinal Status\n"
    "11111111-1111-1111-1111-111111111111\tAlice\t\n"
    "22222222-2222-2222-2222-222222222222\tBob\t\n"
)


@pytest.fixture(autouse=True)
def no_counter_refresh(monkeypatch):
    # The opening counters live in another service with its own database handle.
    monkeypatch.setattr(sheet_import_service, "refresh_opportunity_counts", lambda opportunity_id: _async_value(None))


@pytest.mark.asyncio
async def test_incremental_shortlist_processes_only_unseen_uuid_rows(monkeypatch):
    opportunity = {
        "_id": "opp-a",
        "company_sheet": URL,
        "shortlist_sync": {
            "source_record_ids": ["11111111-1111-1111-1111-111111111111"],
            "shortlisted_student_ids": ["student-a"],
        },
    }
    db = FakeDB()
    imported = []

    async def load(db_arg, opportunity_id):
        assert opportunity_id == "opp-a"
        return opportunity, {"_id": "company-a"}

    async def import_rows(**kwargs):
        imported.append(kwargs["raw_text"])
        return {"mode": "applied", "counts": {"students_matched": 1}}

    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db)
    monkeypatch.setattr(sheet_import_service, "load_opportunity", load)
    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", lambda url: _async_value(SHEET))
    monkeypatch.setattr(sheet_import_service, "build_applicant_index", lambda db, opportunity_id: _async_value([]))
    monkeypatch.setattr(sheet_import_service, "match_applicant", lambda identity, applicants: ({"_id": "student-b"}, False))
    monkeypatch.setattr(sheet_import_service, "import_shortlist", import_rows)

    result = await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-a")

    assert result["rows_processed"] == 1
    assert "Bob" in imported[0]
    assert "Alice" not in imported[0]
    assert result["shortlists_count"] == 2
    assert db.hiring_opportunities.updated


@pytest.mark.asyncio
async def test_incremental_shortlist_rerun_is_idempotent(monkeypatch):
    opportunity = {
        "_id": "opp-a",
        "company_sheet": URL,
        "shortlist_sync": {
            "source_record_ids": ["11111111-1111-1111-1111-111111111111"],
            "shortlisted_student_ids": ["student-a"],
        },
    }
    db = FakeDB()
    calls = []

    async def load(db_arg, opportunity_id):
        return opportunity, {"_id": "company-a"}

    async def import_rows(**kwargs):
        calls.append(kwargs)
        return {"mode": "applied", "counts": {"students_matched": 1}}

    async def update_one(*args, **kwargs):
        update = kwargs.get("update", args[1] if len(args) > 1 else {})
        opportunity.update(update.get("$set", {}))
        db.hiring_opportunities.updated.append((args, kwargs))

    db.hiring_opportunities.update_one = update_one
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db)
    monkeypatch.setattr(sheet_import_service, "load_opportunity", load)
    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", lambda url: _async_value(SHEET))
    monkeypatch.setattr(sheet_import_service, "build_applicant_index", lambda db, opportunity_id: _async_value([]))
    monkeypatch.setattr(sheet_import_service, "match_applicant", lambda identity, applicants: ({"_id": "student-b"}, False))
    monkeypatch.setattr(sheet_import_service, "import_shortlist", import_rows)

    first = await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-a")
    second = await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-a")

    assert first["shortlists_count"] == 2
    assert second["rows_processed"] == 0
    assert second["shortlists_count"] == 2
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_incremental_shortlist_skips_missing_sheet(monkeypatch):
    opportunity = {"_id": "opp-b", "company_sheet": None}
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: FakeDB())
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _async_value((opportunity, {})))

    result = await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-b")

    assert result["mode"] == "skipped"
    assert result["rows_processed"] == 0


@pytest.mark.asyncio
async def test_incremental_shortlist_without_checkpoint_runs_the_full_import(monkeypatch):
    opportunity = {"_id": "opp-e", "company_sheet": URL}
    calls = []
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: FakeDB())
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _async_value((opportunity, {})))

    async def full_import(**kwargs):
        calls.append(kwargs)
        return {"mode": "applied", "counts": {}}

    monkeypatch.setattr(sheet_import_service, "sync_from_sheet", full_import)

    await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-e")

    assert calls == [{"opportunity_id": "opp-e", "kind": "shortlist", "confirm": True, "force": True}]


@pytest.mark.asyncio
async def test_incremental_shortlist_without_uuids_reimports_the_sheet_in_full(monkeypatch):
    opportunity = {
        "_id": "opp-c",
        "company_sheet": URL,
        "shortlist_sync": {"source_record_ids": [], "shortlisted_student_ids": []},
    }
    sheet = "Full Name\tResume\nAlice\thttp://resume\n"
    imported = []
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: FakeDB())
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _async_value((opportunity, {})))
    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", lambda url: _async_value(sheet))

    async def import_rows(**kwargs):
        imported.append(kwargs)
        return {"mode": "applied", "counts": {}}

    monkeypatch.setattr(sheet_import_service, "import_shortlist", import_rows)

    result = await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-c")

    assert imported == [{"opportunity_id": "opp-c", "raw_text": sheet, "confirm": True}]
    assert result["source_url"] == URL


@pytest.mark.asyncio
async def test_incremental_shortlist_does_not_advance_on_import_failure(monkeypatch):
    opportunity = {
        "_id": "opp-d",
        "company_sheet": URL,
        "shortlist_sync": {
            "source_record_ids": ["11111111-1111-1111-1111-111111111111"],
            "shortlisted_student_ids": ["student-a"],
        },
    }
    db = FakeDB()
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db)
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _async_value((opportunity, {})))
    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", lambda url: _async_value(SHEET))
    monkeypatch.setattr(sheet_import_service, "build_applicant_index", lambda db, opportunity_id: _async_value([]))
    monkeypatch.setattr(sheet_import_service, "match_applicant", lambda identity, applicants: ({"_id": "student-b"}, False))

    async def fail(**kwargs):
        raise RuntimeError("import failed")

    monkeypatch.setattr(sheet_import_service, "import_shortlist", fail)

    with pytest.raises(RuntimeError):
        await sheet_import_service.sync_shortlist_sheet_incremental(opportunity_id="opp-d")

    assert db.hiring_opportunities.updated == []


async def _async_value(value):
    return value
