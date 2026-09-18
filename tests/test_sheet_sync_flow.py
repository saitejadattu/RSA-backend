from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.jobs import incremental_sync
from app.routes import admin
from app.services import sheet_import_service

URL = "https://docs.google.com/spreadsheets/d/master/edit"


async def _value(value):
    return value


class NoSheetSettings:
    student_sheet_url = None


class RecordingCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, query, update):
        self.updates.append((query, update))


class RecordingDB:
    def __init__(self):
        self.collection = RecordingCollection()

    def __getitem__(self, name):
        return self.collection


# ---- shared sheet helpers ----

def test_header_only_sheet_is_empty():
    assert sheet_import_service.is_empty_sheet("Timestamp\tName\tPhone\n")
    assert sheet_import_service.is_empty_sheet("")
    assert not sheet_import_service.is_empty_sheet("Timestamp\tName\tPhone\n1/2/2026 10:00:00\tAlice\t9999999999\n")


def test_stored_checkpoint_timestamp_parses():
    assert sheet_import_service.parse_timestamp("2026-01-02T12:00:00Z") == datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_empty_fetched_sheet_is_skipped_not_failed(monkeypatch):
    async def must_not_import(**kwargs):
        raise AssertionError("an empty sheet must not be imported")

    monkeypatch.setattr(sheet_import_service, "import_responses", must_not_import)

    result = await sheet_import_service.import_fetched_sheet(
        RecordingDB(), {"_id": "opp"}, "responses", "Timestamp\tName\tPhone\n", URL, confirm=True,
    )

    assert result["mode"] == "skipped"
    assert "no rows yet" in result["message"]


@pytest.mark.asyncio
async def test_full_response_import_records_the_incremental_checkpoint(monkeypatch):
    db = RecordingDB()

    async def import_rows(**kwargs):
        return {"mode": "applied", "counts": {"applications_to_create": 2}}

    monkeypatch.setattr(sheet_import_service, "import_responses", import_rows)
    text = "Timestamp\tName\tPhone\n1/2/2026 10:00:00\tAlice\t9999999999\n1/3/2026 09:30:00\tBob\t8888888888\n"

    await sheet_import_service.import_fetched_sheet(db, {"_id": "opp"}, "responses", text, URL, confirm=True)

    [(query, update)] = db.collection.updates
    checkpoint = update["$set"]["response_sync"]
    assert query == {"_id": "opp"}
    assert checkpoint["last_processed_response_timestamp"] == "2026-01-03T09:30:00Z"
    assert checkpoint["last_processed_row"] == 3


@pytest.mark.asyncio
async def test_already_imported_opening_is_skipped_without_downloading(monkeypatch):
    opportunity = {"_id": "opp", "student_response_sheet": URL, "responses_imported_at": datetime(2026, 9, 1)}
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: RecordingDB())
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _value((opportunity, {})))

    async def must_not_fetch(url):
        raise AssertionError("an already-imported sheet must not be downloaded")

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", must_not_fetch)

    result = await sheet_import_service.sync_from_sheet(opportunity_id="opp", kind="responses", confirm=True)

    assert result["mode"] == "skipped"


@pytest.mark.asyncio
async def test_changed_link_is_pulled_again(monkeypatch):
    opportunity = {
        "_id": "opp",
        "student_response_sheet": URL,
        "responses_imported_at": datetime(2026, 9, 1),
        "response_sheet_changed_at": datetime(2026, 9, 2),
    }
    fetched = []
    monkeypatch.setattr(sheet_import_service, "get_database", lambda: RecordingDB())
    monkeypatch.setattr(sheet_import_service, "load_opportunity", lambda db, opportunity_id: _value((opportunity, {})))

    async def fetch(url):
        fetched.append(url)
        return "Timestamp\tName\tPhone\n"

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    result = await sheet_import_service.sync_from_sheet(opportunity_id="opp", kind="responses", confirm=True)

    assert fetched == [URL]
    assert result["mode"] == "skipped"  # pulled, but the new sheet has no rows yet


# ---- pasted Master rows ----

MASTER_HEADER = "Opportunity Received On\tReceived Time\tCompany Name\tRole\n"
ROWS = "15-Sep-2026\t10:00 AM\tAcme\tAI Intern\n"


@pytest.mark.asyncio
async def test_pasted_rows_with_header_are_used_as_is(monkeypatch):
    async def must_not_fetch(url):
        raise AssertionError("no header lookup needed")

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", must_not_fetch)

    assert await sheet_import_service.with_master_header(MASTER_HEADER + ROWS, None) == MASTER_HEADER + ROWS


@pytest.mark.asyncio
async def test_pasted_rows_without_header_borrow_the_master_header(monkeypatch):
    async def fetch(url):
        assert url == URL
        return MASTER_HEADER + "1-Sep-2026\t9:00 AM\tOld Co\tSDE\n"

    monkeypatch.setattr(sheet_import_service, "fetch_sheet_text", fetch)

    rows = sheet_import_service.read_response_rows(await sheet_import_service.with_master_header(ROWS, URL))

    pick = sheet_import_service.pick
    assert [(pick(row, "Company Name"), pick(row, "Role")) for row in rows] == [("Acme", "AI Intern")]


@pytest.mark.asyncio
async def test_pasted_rows_without_header_or_link_explain_what_to_do(monkeypatch):
    monkeypatch.setattr(sheet_import_service, "get_settings", lambda: NoSheetSettings())

    with pytest.raises(HTTPException) as error:
        await sheet_import_service.with_master_header(ROWS, None)

    assert error.value.status_code == 422
    assert "header row" in error.value.detail


# ---- the shared pipeline ----

@pytest.mark.asyncio
async def test_full_sync_loads_sheets_by_the_per_opening_rules(monkeypatch):
    calls = []

    async def master(**kwargs):
        assert kwargs == {"url": URL, "confirm": True}
        return {
            "counts": {"opportunities_to_create": 0, "opportunities_to_update": 2, "skipped": 0},
            "processed_opportunities": [
                {"opportunity_id": "done", "is_new": False},
                {"opportunity_id": "never", "is_new": False},
            ],
        }

    async def sheet(**kwargs):
        calls.append((kwargs["kind"], kwargs["opportunity_id"], kwargs.get("force", False)))
        return {"mode": "applied", "counts": {"applications_to_create": 3}}

    monkeypatch.setattr(incremental_sync, "import_master_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", sheet)

    summary = await incremental_sync.run_full_sync(master_url=URL)

    assert summary["status"] == "SUCCESS"
    assert summary["master"]["updated"] == 2
    # Fetch entire sheet re-reads every opening, so an opening imported earlier
    # picks up its new responses and shortlist without anyone pressing Force.
    for opportunity_id in ("done", "never"):
        assert ("responses", opportunity_id, True) in calls
        assert ("shortlist", opportunity_id, True) in calls
    results = {item["opportunity_id"]: item for item in summary["opportunity_results"]}
    assert results["done"]["response"] == {"status": "SUCCESS", "processed": 3, "skipped": 0}
    assert results["never"]["response"] == {"status": "SUCCESS", "processed": 3, "skipped": 0}


@pytest.mark.asyncio
async def test_repeated_master_row_syncs_the_opening_once(monkeypatch):
    calls = []

    async def master(**kwargs):
        return {"processed_opportunities": [
            {"opportunity_id": "opp", "is_new": True},
            {"opportunity_id": "opp", "is_new": False},
        ]}

    async def sheet(**kwargs):
        calls.append(kwargs["kind"])
        return {"mode": "applied", "counts": {}}

    monkeypatch.setattr(incremental_sync, "import_master_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", sheet)

    summary = await incremental_sync.run_full_sync(master_url=URL)

    assert calls == ["responses", "shortlist"]
    assert len(summary["opportunity_results"]) == 1


@pytest.mark.asyncio
async def test_opening_failures_make_a_partial_sync_not_a_failed_one(monkeypatch):
    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "opp", "is_new": True}]}

    async def private_sheet(**kwargs):
        raise HTTPException(status_code=409, detail="This sheet isn't shared publicly, so it can't be fetched.")

    monkeypatch.setattr(incremental_sync, "import_master_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", private_sheet)

    summary = await incremental_sync.run_full_sync(master_url=URL)

    assert summary["status"] == "PARTIAL"
    assert summary["opportunity_results"][0]["response"]["error"].startswith("This sheet isn't shared publicly")
    assert "response import failed for 1 opportunity" in summary["message"]


@pytest.mark.asyncio
async def test_partial_sync_is_returned_as_a_normal_response(monkeypatch):
    async def partial(**kwargs):
        return {"status": "PARTIAL", "message": "Sync completed with failures"}

    monkeypatch.setattr(admin, "run_full_sync", partial)

    response = await admin.full_sheet_sync(admin.MasterIncrementalRequest(url=URL))

    assert response == {"status": "PARTIAL", "message": "Sync completed with failures"}


@pytest.mark.asyncio
async def test_paste_sync_passes_rows_and_link_to_the_master_import(monkeypatch):
    received = {}

    async def paste(**kwargs):
        received.update(kwargs)
        return {"processed_opportunities": []}

    monkeypatch.setattr(incremental_sync, "import_master_paste", paste)

    summary = await incremental_sync.run_paste_sync(raw_text=ROWS, master_url=URL)

    assert received == {"raw_text": ROWS, "url": URL, "confirm": True}
    assert summary["status"] == "SUCCESS"
