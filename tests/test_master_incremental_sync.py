import pytest
from fastapi import HTTPException
from datetime import datetime, timezone

from app.services import sheet_import_service
from app.db.collections import COMPANIES, HIRING_OPPORTUNITIES


@pytest.mark.parametrize("url", [
    "https://docs.google.com/spreadsheets/d/id/edit",
    "https://docs.google.com/spreadsheets/d/id/edit?gid=1914832966",
    "https://docs.google.com/spreadsheets/d/id/edit?gid=1914832966#gid=1914832966",
])
def test_valid_google_sheet_url_forms_are_accepted(url):
    assert sheet_import_service.sheet_export_url(url).startswith("https://docs.google.com/spreadsheets/d/id/export")


class Cursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self, length=None):
        return list(self.items)


class Opportunities:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query, projection=None):
        if "opportunity_received_at" in query:
            documents = [doc for doc in self.documents if doc.get("opportunity_received_at")]
        elif "source_sheet_row" in query and "$gt" in query["source_sheet_row"]:
            documents = [doc for doc in self.documents if doc.get("source_sheet_row", 0) > query["source_sheet_row"]["$gt"]]
        else:
            documents = self.documents
        return Cursor(documents)

    async def find_one(self, query, projection=None):
        return None


class Database:
    def __init__(self, documents):
        self.opportunities = Opportunities(documents)

    def __getitem__(self, name):
        assert name in {HIRING_OPPORTUNITIES, COMPANIES}
        return self.opportunities


class FakeHTTPResponse:
    def __init__(self, text):
        self.status_code = 200
        self.headers = {"content-type": "text/csv; charset=utf-8"}
        self.text = text


class FakeHTTPClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def get(self, url, params):
        self.requests.append(params)
        if params["range"] == "A1:ZZ1":
            return FakeHTTPResponse("Company Name,Role,Opportunity Received On\n")
        return FakeHTTPResponse("Acme,Engineer,22-Aug-2026\n")


@pytest.mark.asyncio
async def test_bounded_master_fetch_requests_csv_and_parses_master_row(monkeypatch):
    FakeHTTPClient.requests = []
    monkeypatch.setattr(sheet_import_service.httpx, "AsyncClient", FakeHTTPClient)

    raw_text = await sheet_import_service.fetch_master_incremental_text(
        "https://docs.google.com/spreadsheets/d/id/edit?gid=123#gid=123", 340, 379
    )
    rows = sheet_import_service.read_response_rows(raw_text)

    assert all(request["tqx"] == "out:csv" for request in FakeHTTPClient.requests)
    assert [request["range"] for request in FakeHTTPClient.requests] == ["A1:ZZ1", "A340:ZZ379"]
    assert rows == [{
        "company_name": "Acme",
        "role": "Engineer",
        "opportunity_received_on": "22-Aug-2026",
    }]


@pytest.mark.asyncio
async def test_incremental_uses_date_checkpoint_and_bounded_window(monkeypatch):
    requested_start = None
    requested_end = None
    imported = {}
    checkpoint = datetime(2026, 8, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: Database([{"source_sheet_row": 350, "opportunity_received_at": checkpoint}]))

    async def fetch(url, start_row, end_row):
        nonlocal requested_start, requested_end
        requested_start = start_row
        requested_end = end_row
        return "Company Name\tRole\tOpportunity Received On\nAcme\tEngineer\t22-Aug-2026\n"

    async def import_rows(*, raw_text, confirm, source_row_offset, collect_opportunity_ids):
        imported.update(raw_text=raw_text, confirm=confirm, source_row_offset=source_row_offset, collect_opportunity_ids=collect_opportunity_ids)
        return {"counts": {"rows": 1, "opportunities_to_create": 1, "opportunities_to_update": 0, "companies_new": 1, "companies_existing": 0, "skipped": 0}, "opportunity_ids": ["new-opp"]}

    monkeypatch.setattr(sheet_import_service, "fetch_master_incremental_text", fetch)
    monkeypatch.setattr(sheet_import_service, "import_master", import_rows)

    result = await sheet_import_service.import_master_incremental_from_url(url="https://docs.google.com/spreadsheets/d/id/edit")

    assert requested_start == 340
    assert requested_end == 379
    assert imported["confirm"] is True
    assert imported["source_row_offset"] == 338
    assert imported["collect_opportunity_ids"] is True
    assert result["rows_scanned"] == 1
    assert result["opportunities_created"] == 1
    assert result["opportunity_ids"] == ["new-opp"]


@pytest.mark.asyncio
async def test_incremental_processes_multiple_new_rows_without_duplicates(monkeypatch):
    checkpoint = datetime(2026, 8, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: Database([{"source_sheet_row": 350, "opportunity_received_at": checkpoint}]))

    async def fetch(url, start_row, end_row):
        return (
            "Company Name\tRole\tOpportunity Received On\n"
            "Company A\tEngineer\t22-Aug-2026\n"
            "Company B\tEngineer\t22-Aug-2026\n"
            "Company A\tEngineer\t22-Aug-2026\n"
        )

    imported_rows = []

    async def import_rows(*, raw_text, confirm, source_row_offset, collect_opportunity_ids):
        imported_rows.extend(sheet_import_service.read_response_rows(raw_text))
        return {"counts": {"rows": len(imported_rows), "opportunities_to_create": 2, "opportunities_to_update": 0, "companies_new": 2, "companies_existing": 0, "skipped": 0}, "opportunity_ids": ["a", "b"]}

    monkeypatch.setattr(sheet_import_service, "fetch_master_incremental_text", fetch)
    monkeypatch.setattr(sheet_import_service, "import_master", import_rows)

    result = await sheet_import_service.import_master_incremental_from_url(url="https://docs.google.com/spreadsheets/d/id/edit")

    assert len(imported_rows) == 3
    assert result["opportunities_created"] == 2


@pytest.mark.asyncio
async def test_incremental_empty_range_is_successful_noop(monkeypatch):
    checkpoint = datetime(2026, 8, 21, tzinfo=timezone.utc)
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: Database([{"source_sheet_row": 350, "opportunity_received_at": checkpoint}]))

    async def fetch_empty(url, start_row, end_row):
        return "Company Name\tRole\n"

    monkeypatch.setattr(sheet_import_service, "fetch_master_incremental_text", fetch_empty)

    result = await sheet_import_service.import_master_incremental_from_url(url="https://docs.google.com/spreadsheets/d/id/edit")

    assert result["mode"] == "incremental"
    assert result["rows_scanned"] == 0
    assert result["rows_processed"] == 0
    assert "No new opportunities found" in result["message"]


@pytest.mark.asyncio
async def test_incremental_requires_full_sync_watermark(monkeypatch):
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: Database([]))

    with pytest.raises(HTTPException) as error:
        await sheet_import_service.import_master_incremental_from_url(url="https://docs.google.com/spreadsheets/d/id/edit")

    assert error.value.status_code == 409
    assert "Fetch entire sheet data first" in str(error.value.detail)


@pytest.mark.asyncio
async def test_incremental_invalid_url_is_clear_error():
    with pytest.raises(HTTPException) as error:
        await sheet_import_service.fetch_master_incremental_text("not-a-sheet", 2)

    assert error.value.status_code == 422
    assert "Master incremental sync failed" in str(error.value.detail)
