"""Sheet sync pipeline: the Master sheet, then each touched opening's responses
and shortlist.

One pipeline with three ways in - Pull only new (incremental), Fetch entire
sheet (full) and pasted Master rows - so every path loads the same data the same
way. Also the standalone Render Cron entry point for the incremental sync.
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from app.config.settings import get_settings
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database  # noqa: F401 - patched in tests
from app.services.sheet_import_service import (
    import_master_from_url,
    import_master_incremental_from_url,
    import_master_paste,
    recent_openings_for_refresh,
    sync_from_sheet,
    sync_response_sheet_incremental,
    sync_shortlist_sheet_incremental,
)

LOGGER = logging.getLogger("auto_sync")

# Openings synced side by side. Sheet downloads dominate a sync, so a few in
# parallel turns minutes into seconds without hammering Google or Mongo.
SHEET_SYNC_CONCURRENCY = 6

# How far back Pull only new refreshes openings it did not just create. Long
# enough to cover an opening still receiving applicants, short enough that the
# run stays quick - a full sweep of every opening is what Fetch entire sheet is for.
RECENT_REFRESH_DAYS = 30


def _stage_summary(status: str, result: dict | None = None, error: Exception | None = None) -> dict[str, Any]:
    result = result or {}
    summary = {"status": status, "error": type(error).__name__ if error else None}
    if isinstance(error, HTTPException):
        summary["error_status"] = error.status_code
        summary["error_detail"] = str(error.detail)
    summary.update(result)
    return summary


def _response_metrics(result: dict) -> dict:
    counts = result.get("counts", {}) if isinstance(result.get("counts"), dict) else {}
    return {
        "processed": result.get("rows_processed", counts.get("applications_to_create", 0) + counts.get("applications_to_update", 0)),
        "skipped": result.get("skipped", counts.get("skipped", 0)),
    }


def _shortlist_metrics(result: dict) -> dict:
    counts = result.get("result", {}).get("counts", {}) if isinstance(result.get("result"), dict) else {}
    counts = result.get("counts", counts) if isinstance(result.get("counts"), dict) else counts
    return {
        "processed": result.get("rows_processed", counts.get("applications_to_mark", counts.get("students_matched", counts.get("rows", 0)))),
        "skipped": result.get("skipped", counts.get("unmatched", 0) + counts.get("ambiguous", 0)),
    }


def _missing_sheet(error: str | None, sheet: str) -> bool:
    return bool(error and f"No {sheet} sheet URL" in error)


def _master_metrics(result: dict) -> dict:
    # Incremental reports opportunities_*; the full and paste imports report counts.
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    return {
        "created": result.get("opportunities_created", counts.get("opportunities_to_create", 0)),
        "updated": result.get("opportunities_updated", counts.get("opportunities_to_update", 0)),
        "restored": result.get("opportunities_restored", counts.get("opportunities_to_restore", 0)),
        "unchanged": result.get("opportunities_unchanged", counts.get("opportunities_unchanged", 0)),
        "skipped": result.get("rows_skipped", counts.get("skipped", 0)),
    }


def describe_failure(summary: dict) -> str:
    """Build an admin-facing reason for a run that did not fully succeed.

    Only HTTPException details are surfaced; other exceptions are reported by
    type so internal messages (connection strings, paths) never leak.
    """
    master = summary.get("master") or {}
    if master.get("status") == "FAILED":
        return master.get("error_detail") or (
            f"Master sheet sync failed ({master.get('error') or 'unknown error'}). Check the backend logs."
        )
    parts = []
    failed_responses = len((summary.get("responses") or {}).get("failed_opportunities", []))
    failed_shortlists = len((summary.get("shortlist") or {}).get("failed_opportunities", []))
    if failed_responses:
        parts.append(f"response import failed for {failed_responses} opportunit{'y' if failed_responses == 1 else 'ies'}")
    if failed_shortlists:
        parts.append(f"shortlist import failed for {failed_shortlists} opportunit{'y' if failed_shortlists == 1 else 'ies'}")
    if not parts:
        return "Sync failed. Check the backend logs."
    return f"Sync completed with failures: {'; '.join(parts)}. See the sync results for each opportunity's error."


async def _run_stage(
    name: str,
    operation: Callable[[], Awaitable[dict]],
    metrics: Callable[[dict], dict],
) -> dict:
    LOGGER.info("[AUTO-SYNC] %s sync started", name)
    try:
        result = await operation()
    except Exception as exc:
        if isinstance(exc, HTTPException):
            LOGGER.error(
                "[AUTO-SYNC] %s sync FAILED (%s %s): %s",
                name, type(exc).__name__, exc.status_code, exc.detail,
            )
        else:
            LOGGER.error("[AUTO-SYNC] %s sync FAILED (%s)", name, type(exc).__name__)
        return _stage_summary("FAILED", error=exc)
    LOGGER.info("[AUTO-SYNC] %s sync completed", name)
    return _stage_summary("SUCCESS", result={**metrics(result), "result": result})


def _stage_outcome(stage: dict, sheet: str, missing_reason: str) -> dict:
    """A stage summary -> the per-opening status shown in the sync results."""
    if stage["status"] == "FAILED":
        error = stage.get("error_detail") or stage.get("error")
        if _missing_sheet(error, sheet):
            return {"status": "SKIPPED", "reason": missing_reason}
        return {"status": "FAILED", "error": error}
    result = stage.get("result") or {}
    if result.get("mode") == "skipped":
        return {"status": "SKIPPED", "reason": result.get("message") or "Nothing to import."}
    return {"status": "SUCCESS", "processed": stage["processed"], "skipped": stage["skipped"]}


def _brought_new_applicants(result: dict) -> bool:
    """Did this response import add anyone who was not on the opening before?

    A late applicant may already sit on the shortlist, on a row an earlier sync
    has read. The shortlist is then re-read in full so they are still marked.
    """
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    return bool(result.get("applications_created") or counts.get("applications_to_create"))


async def _sync_opening(processed: dict, *, refresh_existing: bool) -> dict:
    """Responses, then shortlist, for one opening.

    Nothing here needs a hand: an opening that is new, restored from a delete, or
    being re-read by Fetch entire sheet is imported in full (which also re-runs
    the shortlist check). Pull only new reads just the new rows of an opening it
    has already imported, and falls back to a full read when those new rows bring
    applicants the shortlist may already list.
    """
    opportunity_id = processed["opportunity_id"]
    is_new = bool(processed.get("is_new"))
    restored = bool(processed.get("restored"))
    # A restored opening was invisible while it was deleted, so its sheets are
    # re-read from the top, exactly like a brand-new one.
    reload_in_full = is_new or restored or not refresh_existing
    label = f"{processed.get('company') or 'Unknown company'} / {processed.get('role') or 'Unknown role'}"
    outcome = {
        "opportunity_id": opportunity_id,
        "is_new": is_new,
        "restored": restored,
        "master": processed.get("master") or {"status": "created" if is_new else "updated"},
        "company": processed.get("company"),
        "role": processed.get("role"),
        "received_on": processed.get("received_on"),
    }
    if processed.get("response_url_present") is False:
        outcome["response"] = {"status": "SKIPPED", "reason": "No response sheet URL is stored on this opportunity."}
        outcome["shortlist"] = {"status": "SKIPPED", "reason": "Skipped because response import was not performed."}
        return outcome

    if reload_in_full:
        response_operation = lambda: sync_from_sheet(opportunity_id=opportunity_id, kind="responses", confirm=True, force=True)  # noqa: E731
    else:
        response_operation = lambda: sync_response_sheet_incremental(opportunity_id=opportunity_id)  # noqa: E731
    response_stage = await _run_stage(f"Response {label}", response_operation, _response_metrics)
    outcome["response"] = _stage_outcome(response_stage, "response", "Response sheet URL missing")
    if response_stage["status"] == "FAILED":
        outcome["shortlist"] = {"status": "SKIPPED", "reason": "Response import failed; shortlist was not attempted."}
        return outcome

    if processed.get("shortlist_url_present") is False:
        outcome["shortlist"] = {"status": "SKIPPED", "reason": "No shortlist sheet URL is stored on this opportunity."}
        return outcome

    # A full response import can bring in applicants the shortlist has never
    # been matched against, so the shortlist is re-imported in full after it.
    response_result = response_stage.get("result") or {}
    if reload_in_full or response_result.get("mode") == "applied" or _brought_new_applicants(response_result):
        shortlist_operation = lambda: sync_from_sheet(opportunity_id=opportunity_id, kind="shortlist", confirm=True, force=True)  # noqa: E731
    else:
        shortlist_operation = lambda: sync_shortlist_sheet_incremental(opportunity_id=opportunity_id)  # noqa: E731
    shortlist_stage = await _run_stage(f"Shortlist {label}", shortlist_operation, _shortlist_metrics)
    outcome["shortlist"] = _stage_outcome(shortlist_stage, "company / shortlist", "Shortlist sheet URL missing")
    return outcome


def _unique_openings(processed_opportunities: list[dict]) -> list[dict]:
    """A Master row repeated in the sheet maps to one opening - sync it once, so
    two tasks never import the same opening at the same time."""
    unique: dict[str, dict] = {}
    for processed in processed_opportunities:
        seen = unique.get(processed["opportunity_id"])
        if seen is None:
            unique[processed["opportunity_id"]] = dict(processed)
        elif processed.get("is_new"):
            seen["is_new"] = True
    return list(unique.values())


def _tally(summary: dict, outcome: dict) -> None:
    for key, stage in (("responses", outcome["response"]), ("shortlist", outcome["shortlist"])):
        bucket = summary[key]
        if stage["status"] == "FAILED":
            bucket["failed_opportunities"].append(outcome["opportunity_id"])
        elif stage["status"] == "SKIPPED":
            bucket["skipped_opportunities"].append({"opportunity_id": outcome["opportunity_id"], "reason": stage["reason"]})
        else:
            bucket["processed"] += stage["processed"]
            bucket["skipped"] += stage["skipped"]
    summary["opportunity_results"].append(outcome)


async def run_sheet_sync(*, mode: str, master_operation: Callable[[], Awaitable[dict]]) -> dict:
    """Run the Master stage, then responses -> shortlist for every opening it touched.

    Each opening is isolated, so one failure does not stop the others. status is
    SUCCESS, PARTIAL (Master fine, some openings failed) or FAILED (Master failed).
    """
    started_at = datetime.now(timezone.utc)
    started_clock = monotonic()
    LOGGER.info("[AUTO-SYNC] Started (%s)", mode)
    summary: dict[str, Any] = {
        "mode": mode,
        "started_at": started_at,
        "completed_at": None,
        "duration": None,
        "master": None,
        "responses": {"status": "SUCCESS", "processed": 0, "skipped": 0, "failed_opportunities": [], "skipped_opportunities": []},
        "shortlist": {"status": "SUCCESS", "processed": 0, "skipped": 0, "failed_opportunities": [], "skipped_opportunities": []},
        "opportunity_results": [],
    }

    summary["master"] = await _run_stage("Master", master_operation, _master_metrics)
    master_result = summary["master"].get("result") or {}
    processed_opportunities = master_result.get("processed_opportunities")
    if processed_opportunities is None:
        processed_opportunities = [
            {"opportunity_id": opportunity_id, "is_new": False}
            for opportunity_id in master_result.get("opportunity_ids", [])
        ]
    openings = _unique_openings(processed_opportunities)

    # Pull only new also refreshes the openings received recently: they are
    # already in the database, so the Master stage does not return them, but they
    # are the ones still collecting responses and shortlists. Without this an
    # admin has to open each one and press Force.
    if mode == "incremental":
        refreshed = await recent_openings_for_refresh(
            days=RECENT_REFRESH_DAYS, exclude_ids={opening["opportunity_id"] for opening in openings}
        )
        LOGGER.info("[AUTO-SYNC] %d recent openings also being refreshed", len(refreshed))
        summary["refreshed_openings"] = len(refreshed)
        openings.extend(refreshed)

    LOGGER.info("[AUTO-SYNC] %d openings to sync responses and shortlists for", len(openings))

    semaphore = asyncio.Semaphore(SHEET_SYNC_CONCURRENCY)
    refresh_existing = mode == "incremental"

    async def sync_one(processed: dict) -> dict:
        async with semaphore:
            return await _sync_opening(processed, refresh_existing=refresh_existing)

    for outcome in await asyncio.gather(*(sync_one(processed) for processed in openings)):
        _tally(summary, outcome)

    if summary["responses"]["failed_opportunities"]:
        summary["responses"]["status"] = "FAILED"
    if summary["shortlist"]["failed_opportunities"]:
        summary["shortlist"]["status"] = "FAILED"
    summary["completed_at"] = datetime.now(timezone.utc)
    summary["duration"] = round(monotonic() - started_clock, 3)
    if summary["master"]["status"] == "FAILED":
        summary["status"] = "FAILED"
    elif summary["responses"]["status"] == "FAILED" or summary["shortlist"]["status"] == "FAILED":
        summary["status"] = "PARTIAL"
    else:
        summary["status"] = "SUCCESS"
    if summary["status"] != "SUCCESS":
        summary["message"] = describe_failure(summary)
    LOGGER.info("[AUTO-SYNC] Completed: %s in %.1fs", summary["status"], summary["duration"])
    return summary


async def run_incremental_sync(*, master_url: str | None = None) -> dict:
    """Pull only new: new Master rows, then their responses and shortlists."""
    url = (master_url or get_settings().student_sheet_url or "").strip()
    return await run_sheet_sync(mode="incremental", master_operation=lambda: import_master_incremental_from_url(url=url))


async def run_full_sync(*, master_url: str | None = None) -> dict:
    """Fetch entire sheet: every Master row, then responses and shortlists for
    the openings that were never imported or whose sheet link changed."""
    url = (master_url or get_settings().student_sheet_url or "").strip()
    return await run_sheet_sync(mode="full", master_operation=lambda: import_master_from_url(url=url, confirm=True))


async def run_paste_sync(*, raw_text: str, master_url: str | None = None) -> dict:
    """Pasted Master rows (header optional), then their responses and shortlists."""
    return await run_sheet_sync(
        mode="paste",
        master_operation=lambda: import_master_paste(raw_text=raw_text, url=master_url, confirm=True),
    )


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    _configure_logging()
    try:
        summary = asyncio.run(_run_with_cleanup())
    except Exception:
        LOGGER.exception("[AUTO-SYNC] Fatal setup failure")
        return 1
    LOGGER.info("[AUTO-SYNC] Summary: %s", summary)
    return 0 if summary["status"] == "SUCCESS" else 1


async def _run_with_cleanup() -> dict:
    await connect_to_mongo()
    try:
        return await run_incremental_sync()
    finally:
        await close_mongo_connection()


if __name__ == "__main__":
    sys.exit(main())
