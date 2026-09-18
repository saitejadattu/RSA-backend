import json

import pytest
from fastapi import HTTPException

from app.jobs import incremental_sync
from app.routes import admin
from app.schemas.admin import MasterIncrementalRequest


class FakeOpportunities:
    def __init__(self, ids):
        self.ids = ids

    def find(self, *args, **kwargs):
        return self

    def to_list(self, length=None):
        return _async_value([{"_id": value} for value in self.ids])


class FakeDB:
    def __init__(self, ids):
        self.opportunities = FakeOpportunities(ids)

    def __getitem__(self, name):
        assert name == "hiring_opportunities"
        return self.opportunities


class Settings:
    student_sheet_url = "https://docs.google.com/spreadsheets/d/master/edit"


@pytest.fixture(autouse=True)
def no_recent_refresh(monkeypatch):
    """Pull only new also refreshes recently received openings, which reads the
    database. Tests that care about it patch this with their own list."""
    async def none(**kwargs):
        return []

    monkeypatch.setattr(incremental_sync, "recent_openings_for_refresh", none)


@pytest.mark.asyncio
async def test_job_runs_master_response_shortlist_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())
    monkeypatch.setattr(incremental_sync, "get_database", lambda: FakeDB(["opp-1", "opp-2"]))

    async def master(**kwargs):
        calls.append("master")
        return {"opportunities_created": 1, "opportunities_updated": 2, "rows_skipped": 3, "opportunity_ids": ["opp-1", "opp-2"]}

    async def response(**kwargs):
        calls.append(f"response:{kwargs['opportunity_id']}")
        return {"rows_processed": 4, "skipped": 5}

    async def shortlist(**kwargs):
        calls.append(f"shortlist:{kwargs['opportunity_id']}")
        return {"rows_processed": 6, "skipped": 7}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist)

    summary = await incremental_sync.run_incremental_sync()

    # Openings sync side by side; within one opening, response always precedes shortlist.
    assert calls[0] == "master"
    assert sorted(calls[1:]) == ["response:opp-1", "response:opp-2", "shortlist:opp-1", "shortlist:opp-2"]
    for opportunity_id in ("opp-1", "opp-2"):
        assert calls.index(f"response:{opportunity_id}") < calls.index(f"shortlist:{opportunity_id}")
    assert summary["status"] == "SUCCESS"
    assert summary["master"]["created"] == 1
    assert summary["responses"]["processed"] == 8
    assert summary["shortlist"]["processed"] == 12


@pytest.mark.asyncio
async def test_job_passes_selected_master_url_to_incremental_service(monkeypatch):
    selected_url = "https://docs.google.com/spreadsheets/d/1pEaRf5JROVQ3YSdL9KFgEtxNYDRCh2NnJEc19eY5v44/edit?gid=1914832966#gid=1914832966"
    received = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        received.append(kwargs["url"])
        return {"opportunity_ids": []}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)

    await incremental_sync.run_incremental_sync(master_url=selected_url)

    assert received == [selected_url]


@pytest.mark.asyncio
async def test_job_attempts_later_stages_after_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())
    monkeypatch.setattr(incremental_sync, "get_database", lambda: FakeDB(["opp-1"]))

    async def master(**kwargs):
        calls.append("master")
        return {"opportunity_ids": ["opp-1"]}

    async def response(**kwargs):
        calls.append("response")
        raise ValueError("response failure")

    async def shortlist(**kwargs):
        calls.append("shortlist")
        return {"rows_processed": 1, "skipped": 0}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist)

    summary = await incremental_sync.run_incremental_sync()

    assert calls == ["master", "response"]
    # The Master stage succeeded, so a per-opening failure makes the run partial.
    assert summary["status"] == "PARTIAL"
    assert summary["master"]["status"] == "SUCCESS"
    assert summary["responses"]["status"] == "FAILED"
    assert summary["shortlist"]["status"] == "SUCCESS"
    assert summary["responses"]["failed_opportunities"] == ["opp-1"]
    assert summary["shortlist"]["skipped_opportunities"][0]["opportunity_id"] == "opp-1"


@pytest.mark.asyncio
async def test_job_does_not_touch_existing_opportunities(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"opportunity_ids": ["new-opp"]}

    async def response(**kwargs):
        calls.append(("response", kwargs["opportunity_id"]))
        return {"rows_processed": 0, "skipped": 0}

    async def shortlist(**kwargs):
        calls.append(("shortlist", kwargs["opportunity_id"]))
        return {"rows_processed": 0, "skipped": 0}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist)

    summary = await incremental_sync.run_incremental_sync()

    assert summary["status"] == "SUCCESS"
    assert calls == [("response", "new-opp"), ("shortlist", "new-opp")]


@pytest.mark.asyncio
async def test_new_opportunities_use_full_response_then_shortlist(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "new-opp", "is_new": True}]}

    async def full_sync(**kwargs):
        calls.append((kwargs["kind"], kwargs["opportunity_id"], kwargs["force"]))
        return {"counts": {"rows": 1}}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)

    summary = await incremental_sync.run_incremental_sync()

    assert calls == [("responses", "new-opp", True), ("shortlist", "new-opp", True)]
    assert summary["opportunity_results"][0]["response"]["status"] == "SUCCESS"
    assert summary["opportunity_results"][0]["shortlist"]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_new_opportunity_response_failure_skips_shortlist(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "new-opp", "is_new": True}]}

    async def full_sync(**kwargs):
        calls.append(kwargs["kind"])
        if kwargs["kind"] == "responses":
            raise RuntimeError("response unavailable")
        return {"counts": {"rows": 1}}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)

    summary = await incremental_sync.run_incremental_sync()

    assert calls == ["responses"]
    assert summary["opportunity_results"][0]["response"]["status"] == "FAILED"
    assert summary["opportunity_results"][0]["shortlist"]["status"] == "SKIPPED"


@pytest.mark.asyncio
async def test_new_opportunity_missing_response_url_skips_shortlist_without_global_failure(monkeypatch):
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "new-opp", "is_new": True}]}

    async def full_sync(**kwargs):
        raise HTTPException(status_code=409, detail="No response sheet URL is stored on this opening.")

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)

    summary = await incremental_sync.run_incremental_sync()

    result = summary["opportunity_results"][0]
    assert summary["status"] == "SUCCESS"
    assert result["response"] == {"status": "SKIPPED", "reason": "Response sheet URL missing"}
    assert result["shortlist"]["status"] == "SKIPPED"


@pytest.mark.asyncio
async def test_new_opportunity_missing_shortlist_url_is_reported_after_response(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "new-opp", "is_new": True}]}

    async def full_sync(**kwargs):
        calls.append(kwargs["kind"])
        if kwargs["kind"] == "shortlist":
            raise HTTPException(status_code=409, detail="No company / shortlist sheet URL is stored on this opening.")
        return {"counts": {"applications_to_create": 2}}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)

    summary = await incremental_sync.run_incremental_sync()

    result = summary["opportunity_results"][0]
    assert calls == ["responses", "shortlist"]
    assert result["response"]["status"] == "SUCCESS"
    assert result["shortlist"] == {"status": "SKIPPED", "reason": "Shortlist sheet URL missing"}


@pytest.mark.asyncio
async def test_existing_opportunity_uses_incremental_services(monkeypatch):
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "existing-opp", "is_new": False}]}

    async def response(**kwargs):
        calls.append(("response_incremental", kwargs["opportunity_id"]))
        return {"rows_processed": 1, "skipped": 0}

    async def shortlist(**kwargs):
        calls.append(("shortlist_incremental", kwargs["opportunity_id"]))
        return {"rows_processed": 1, "skipped": 0}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist)

    await incremental_sync.run_incremental_sync()

    assert calls == [("response_incremental", "existing-opp"), ("shortlist_incremental", "existing-opp")]


@pytest.mark.asyncio
async def test_recent_openings_are_refreshed_alongside_new_rows(monkeypatch):
    """Hands-free: openings the Master stage did not touch, but which are recent
    enough to still be collecting applicants, are refreshed in the same run."""
    calls = []
    excluded = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "new-opp", "is_new": True}]}

    async def recent(*, days, exclude_ids):
        excluded.append((days, sorted(exclude_ids)))
        return [{"opportunity_id": "recent-opp", "is_new": False, "refresh": True}]

    async def full_sync(**kwargs):
        calls.append((kwargs["kind"], kwargs["opportunity_id"], kwargs.get("force")))
        return {"counts": {"rows": 1}}

    async def response_inc(**kwargs):
        calls.append(("response_incremental", kwargs["opportunity_id"], None))
        return {"rows_processed": 1, "skipped": 0}

    async def shortlist_inc(**kwargs):
        calls.append(("shortlist_incremental", kwargs["opportunity_id"], None))
        return {"rows_processed": 1, "skipped": 0}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "recent_openings_for_refresh", recent)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response_inc)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist_inc)

    summary = await incremental_sync.run_incremental_sync()

    assert excluded == [(incremental_sync.RECENT_REFRESH_DAYS, ["new-opp"])]
    # The new opening is pulled in full; the recent one only reads its new rows.
    assert ("responses", "new-opp", True) in calls
    assert ("response_incremental", "recent-opp", None) in calls
    assert ("shortlist_incremental", "recent-opp", None) in calls
    assert summary["refreshed_openings"] == 1


@pytest.mark.asyncio
async def test_restored_opening_is_pulled_in_full(monkeypatch):
    """An opening that was deleted and has come back was invisible while it was
    gone, so its sheets are re-read from the top rather than incrementally."""
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [
            {"opportunity_id": "restored-opp", "is_new": False, "restored": True,
             "master": {"status": "restored"}},
        ]}

    async def full_sync(**kwargs):
        calls.append((kwargs["kind"], kwargs["force"]))
        return {"counts": {"rows": 1}}

    async def never(**kwargs):
        raise AssertionError("a restored opening must not be read incrementally")

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", never)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", never)

    summary = await incremental_sync.run_incremental_sync()

    assert calls == [("responses", True), ("shortlist", True)]
    assert summary["opportunity_results"][0]["master"]["status"] == "restored"
    assert summary["opportunity_results"][0]["restored"] is True


@pytest.mark.asyncio
async def test_late_applicant_triggers_a_full_shortlist_reread(monkeypatch):
    """New applicants may already sit on shortlist rows an earlier sync read, so
    the shortlist is re-read in full instead of only its unseen rows."""
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "existing-opp", "is_new": False}]}

    async def response_inc(**kwargs):
        calls.append("response_incremental")
        return {"mode": "incremental", "rows_processed": 1, "skipped": 0, "applications_created": 1}

    async def full_sync(**kwargs):
        calls.append((kwargs["kind"], kwargs["force"]))
        return {"counts": {"rows": 1}}

    async def shortlist_inc(**kwargs):
        raise AssertionError("new applicants must force a full shortlist re-read")

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response_inc)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist_inc)

    await incremental_sync.run_incremental_sync()

    assert calls == ["response_incremental", ("shortlist", True)]


@pytest.mark.asyncio
async def test_full_sync_re_reads_existing_openings_without_force_by_hand(monkeypatch):
    """Fetch entire sheet is hands-free: an already-imported opening is pulled
    again instead of returning "already imported - use Force"."""
    calls = []
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {"processed_opportunities": [{"opportunity_id": "existing-opp", "is_new": False}]}

    async def full_sync(**kwargs):
        calls.append((kwargs["kind"], kwargs["force"]))
        return {"counts": {"rows": 1}}

    async def never(**kwargs):
        raise AssertionError("a full sync must not read incrementally")

    monkeypatch.setattr(incremental_sync, "import_master_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", never)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", never)

    await incremental_sync.run_full_sync(master_url="https://docs.google.com/spreadsheets/d/master/edit")

    assert calls == [("responses", True), ("shortlist", True)]


def test_main_returns_nonzero_when_pipeline_fails(monkeypatch):
    async def failed_run():
        return {"status": "FAILED"}

    monkeypatch.setattr(incremental_sync, "_run_with_cleanup", failed_run)

    assert incremental_sync.main() == 1


@pytest.mark.asyncio
async def test_admin_endpoint_reports_pipeline_failure_with_non_2xx(monkeypatch):
    async def failed_run(**kwargs):
        return {"status": "FAILED", "responses": {"failed_opportunities": ["opp-1"]}}

    monkeypatch.setattr(admin, "run_incremental_sync", failed_run)

    response = await admin.manual_incremental_sync()

    assert response.status_code == 502
    body = json.loads(response.body)
    assert body["status"] == "FAILED"
    assert body["responses"] == {"failed_opportunities": ["opp-1"]}
    assert body["detail"] == (
        "Sync completed with failures: response import failed for 1 opportunity. "
        "See the sync results for each opportunity's error."
    )


@pytest.mark.asyncio
async def test_admin_endpoint_surfaces_master_failure_reason(monkeypatch):
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        raise HTTPException(status_code=409, detail="Master incremental sync failed: the sheet is not publicly accessible.")

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(admin, "run_incremental_sync", incremental_sync.run_incremental_sync)

    response = await admin.manual_incremental_sync()

    assert response.status_code == 502
    assert json.loads(response.body)["detail"] == "Master incremental sync failed: the sheet is not publicly accessible."


def test_describe_failure_hides_non_http_master_errors():
    summary = {"master": {"status": "FAILED", "error": "RuntimeError"}}

    assert incremental_sync.describe_failure(summary) == (
        "Master sheet sync failed (RuntimeError). Check the backend logs."
    )


@pytest.mark.asyncio
async def test_admin_endpoint_passes_selected_master_url(monkeypatch):
    selected_url = "https://docs.google.com/spreadsheets/d/1pEaRf5JROVQ3YSdL9KFgEtxNYDRCh2NnJEc19eY5v44/edit?gid=1914832966#gid=1914832966"
    received = []

    async def successful_run(**kwargs):
        received.append(kwargs["master_url"])
        return {"status": "SUCCESS"}

    monkeypatch.setattr(admin, "run_incremental_sync", successful_run)

    response = await admin.manual_incremental_sync(MasterIncrementalRequest(url=selected_url))

    assert response == {"status": "SUCCESS"}
    assert received == [selected_url]


@pytest.mark.asyncio
async def test_stage_failures_do_not_expose_error_messages():
    async def fail():
        raise RuntimeError("mongodb://secret.example/password")

    result = await incremental_sync._run_stage("Response", fail, lambda value: {})

    assert result["status"] == "FAILED"
    assert result["error"] == "RuntimeError"
    assert "secret" not in str(result)


@pytest.mark.asyncio
async def test_http_failures_include_safe_status_and_detail():
    async def fail():
        raise HTTPException(status_code=409, detail="run the manual full import first")

    result = await incremental_sync._run_stage("Response", fail, lambda value: {})

    assert result["status"] == "FAILED"
    assert result["error"] == "HTTPException"
    assert result["error_status"] == 409
    assert result["error_detail"] == "run the manual full import first"


@pytest.mark.asyncio
async def test_opportunity_results_include_master_created_and_updated_statuses(monkeypatch):
    monkeypatch.setattr(incremental_sync, "get_settings", lambda: Settings())

    async def master(**kwargs):
        return {
            "processed_opportunities": [
                {"opportunity_id": "new-opp-1", "is_new": True, "company": "Company A", "role": "Role A"},
                {"opportunity_id": "updated-opp-2", "is_new": False, "company": "Company B", "role": "Role B"},
            ]
        }

    async def full_sync(**kwargs):
        return {"counts": {"rows": 1}}

    async def response_inc(**kwargs):
        return {"rows_processed": 1, "skipped": 0}

    async def shortlist_inc(**kwargs):
        return {"rows_processed": 1, "skipped": 0}

    monkeypatch.setattr(incremental_sync, "import_master_incremental_from_url", master)
    monkeypatch.setattr(incremental_sync, "sync_from_sheet", full_sync)
    monkeypatch.setattr(incremental_sync, "sync_response_sheet_incremental", response_inc)
    monkeypatch.setattr(incremental_sync, "sync_shortlist_sheet_incremental", shortlist_inc)

    summary = await incremental_sync.run_incremental_sync()

    results = summary["opportunity_results"]
    assert len(results) == 2
    assert results[0]["opportunity_id"] == "new-opp-1"
    assert results[0]["master"] == {"status": "created"}
    assert results[0]["response"]["status"] == "SUCCESS"
    assert results[0]["shortlist"]["status"] == "SUCCESS"

    assert results[1]["opportunity_id"] == "updated-opp-2"
    assert results[1]["master"] == {"status": "updated"}
    assert results[1]["response"]["status"] == "SUCCESS"
    assert results[1]["shortlist"]["status"] == "SUCCESS"


async def _async_value(value):
    return value
