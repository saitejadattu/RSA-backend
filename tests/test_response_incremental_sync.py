from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.services import sheet_import_service

URL = "https://docs.google.com/spreadsheets/d/abc/edit"


class FakeCollection:
    def __init__(self):
        self.updated = []

    async def update_one(self, *args, **kwargs):
        self.updated.append((args, kwargs))
        return type("Result", (), {"matched_count": 1})()


class FakeDB:
    def __init__(self):
        self.hiring_opportunities = FakeCollection()

    def __getitem__(self, name):
        assert name == "hiring_opportunities"
        return self.hiring_opportunities


def use_opportunity(monkeypatch, opportunity, db=None):
    async def fake_load_opportunity(db_arg, opportunity_id):
        return opportunity, {}

    monkeypatch.setattr(sheet_import_service, "load_opportunity", fake_load_opportunity)
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db or FakeDB())


@pytest.mark.asyncio
async def test_incremental_response_fetches_only_new_rows(monkeypatch):
    opportunity = {
        "_id": "opp-1",
        "student_response_sheet": URL,
        "response_sync": {"last_processed_response_timestamp": "2026-01-02T12:00:00Z", "last_processed_row": 2},
    }
    db = FakeDB()
    use_opportunity(monkeypatch, opportunity, db)

    async def fetch_public_sheet(url):
        return "Timestamp\tName\tPhone\n2026-01-03 12:00:00\tAlice\t9999999999\n2026-01-04 12:00:00\tBob\t8888888888\n"

    async def import_rows(*, opportunity_id, raw_text, confirm, replace):
        assert opportunity_id == "opp-1"
        assert confirm is True
        assert replace is False
        assert "2026-01-03" in raw_text and "2026-01-04" in raw_text
        return {"mode": "applied", "counts": {"rows": 2, "applications_to_create": 2, "applications_to_update": 0, "skipped": 0}}

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch_public_sheet)
    monkeypatch.setattr(sheet_import_service, "import_responses", import_rows)

    result = await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-1")

    assert result["mode"] == "incremental"
    assert result["rows_scanned"] == 2
    assert result["rows_processed"] == 2
    assert db.hiring_opportunities.updated


@pytest.mark.asyncio
async def test_incremental_response_honours_the_saved_checkpoint(monkeypatch):
    # The checkpoint is stored as ISO 8601; it used to parse as None, so every
    # row counted as new and the whole sheet was re-imported each time.
    opportunity = {
        "_id": "opp-5",
        "student_response_sheet": URL,
        "response_sync": {"last_processed_response_timestamp": "2026-01-02T12:00:00Z", "last_processed_row": 3},
    }
    use_opportunity(monkeypatch, opportunity)
    imported = []

    async def fetch_public_sheet(url):
        return (
            "Timestamp\tName\tPhone\n"
            "2026-01-01 09:00:00\tOld\t7777777777\n"
            "2026-01-02 12:00:00\tEdge\t6666666666\n"
            "2026-01-03 12:00:00\tNew\t9999999999\n"
        )

    async def import_rows(*, opportunity_id, raw_text, confirm, replace):
        imported.append(raw_text)
        return {"mode": "applied", "counts": {"applications_to_create": 1}}

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch_public_sheet)
    monkeypatch.setattr(sheet_import_service, "import_responses", import_rows)

    await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-5")

    assert "New" in imported[0]
    assert "Old" not in imported[0] and "Edge" not in imported[0]


@pytest.mark.asyncio
async def test_incremental_response_without_checkpoint_runs_the_full_import(monkeypatch):
    # Imported before checkpoints were recorded - this used to fail with a 409
    # telling the admin to run a full import that never created the checkpoint.
    opportunity = {"_id": "opp-4", "student_response_sheet": URL, "responses_imported_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}
    use_opportunity(monkeypatch, opportunity)
    calls = []

    async def full_import(**kwargs):
        calls.append(kwargs)
        return {"mode": "applied", "counts": {}}

    monkeypatch.setattr(sheet_import_service, "sync_from_sheet", full_import)

    await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-4")

    assert calls == [{"opportunity_id": "opp-4", "kind": "responses", "confirm": True, "force": True}]


@pytest.mark.asyncio
async def test_incremental_response_after_link_change_runs_the_full_import(monkeypatch):
    opportunity = {
        "_id": "opp-6",
        "student_response_sheet": URL,
        "responses_imported_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "response_sheet_changed_at": datetime(2026, 9, 2, tzinfo=timezone.utc),
        "response_sync": {"last_processed_response_timestamp": "2026-08-30T10:00:00Z", "last_processed_row": 40},
    }
    use_opportunity(monkeypatch, opportunity)
    calls = []

    async def full_import(**kwargs):
        calls.append(kwargs["force"])
        return {"mode": "applied", "counts": {}}

    monkeypatch.setattr(sheet_import_service, "sync_from_sheet", full_import)

    await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-6")

    assert calls == [True]


@pytest.mark.asyncio
async def test_incremental_response_empty_range_is_successful_noop(monkeypatch):
    opportunity = {
        "_id": "opp-2",
        "student_response_sheet": URL,
        "response_sync": {"last_processed_response_timestamp": "2026-01-05T00:00:00Z", "last_processed_row": 10},
    }
    use_opportunity(monkeypatch, opportunity)

    async def fetch_public_sheet(url):
        return "Timestamp\tName\tPhone\n"

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch_public_sheet)

    result = await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-2")

    assert result["mode"] == "incremental"
    assert result["rows_scanned"] == 0
    assert result["rows_processed"] == 0
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_incremental_response_does_not_advance_cursor_on_failed_fetch(monkeypatch):
    opportunity = {
        "_id": "opp-3",
        "student_response_sheet": URL,
        "response_sync": {"last_processed_response_timestamp": "2026-01-06T00:00:00Z", "last_processed_row": 7},
    }
    db = FakeDB()
    use_opportunity(monkeypatch, opportunity, db)

    async def fail_public_sheet(url):
        raise HTTPException(status_code=502, detail="Google Sheet unavailable")

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fail_public_sheet)

    with pytest.raises(HTTPException):
        await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-3")

    assert db.hiring_opportunities.updated == []
