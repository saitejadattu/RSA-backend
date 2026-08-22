from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.services import sheet_import_service


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


@pytest.mark.asyncio
async def test_incremental_response_fetches_only_new_rows(monkeypatch):
    opportunity = {
        "_id": "opp-1",
        "student_response_sheet": "https://docs.google.com/spreadsheets/d/abc/edit",
        "response_sync": {"last_processed_response_timestamp": "2026-01-02T12:00:00Z", "last_processed_row": 2},
    }
    db = FakeDB()
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db)

    async def fake_load_opportunity(db_arg, opportunity_id):
        return opportunity, {}

    monkeypatch.setattr(sheet_import_service, "load_opportunity", fake_load_opportunity)

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
async def test_incremental_response_empty_range_is_successful_noop(monkeypatch):
    opportunity = {
        "_id": "opp-2",
        "student_response_sheet": "https://docs.google.com/spreadsheets/d/abc/edit",
        "response_sync": {"last_processed_response_timestamp": "2026-01-05T00:00:00Z", "last_processed_row": 10},
    }
    async def fake_load_opportunity(db_arg, opportunity_id):
        return opportunity, {}

    monkeypatch.setattr(sheet_import_service, "load_opportunity", fake_load_opportunity)
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: FakeDB())

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
        "student_response_sheet": "https://docs.google.com/spreadsheets/d/abc/edit",
        "response_sync": {"last_processed_response_timestamp": "2026-01-06T00:00:00Z", "last_processed_row": 7},
    }
    db = FakeDB()
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: db)

    async def fake_load_opportunity(db_arg, opportunity_id):
        return opportunity, {}

    monkeypatch.setattr(sheet_import_service, "load_opportunity", fake_load_opportunity)

    async def fail_public_sheet(url):
        raise HTTPException(status_code=502, detail="Google Sheet unavailable")

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fail_public_sheet)

    with pytest.raises(HTTPException):
        await sheet_import_service.sync_response_sheet_incremental(opportunity_id="opp-3")

    assert db.hiring_opportunities.updated == []
