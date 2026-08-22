"""Standalone Render Cron entry point for incremental sheet synchronization."""
import asyncio
import logging
import sys
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Awaitable, Callable

from fastapi import HTTPException

from app.config.settings import get_settings
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database
from app.services.sheet_import_service import (
    import_master_incremental_from_url,
    sync_from_sheet,
    sync_response_sheet_incremental,
    sync_shortlist_sheet_incremental,
)

LOGGER = logging.getLogger("auto_sync")


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
    return {
        "created": result.get("opportunities_created", 0),
        "updated": result.get("opportunities_updated", 0),
        "skipped": result.get("rows_skipped", 0),
    }


async def _run_stage(
    name: str,
    operation: Callable[[], Awaitable[dict]],
    metrics: Callable[[dict], dict],
) -> dict:
    LOGGER.info("[AUTO-SYNC] %s incremental sync started", name)
    try:
        result = await operation()
    except Exception as exc:
        if isinstance(exc, HTTPException):
            LOGGER.error(
                "[AUTO-SYNC] %s incremental sync FAILED (%s %s): %s",
                name, type(exc).__name__, exc.status_code, exc.detail,
            )
        else:
            LOGGER.error("[AUTO-SYNC] %s incremental sync FAILED (%s)", name, type(exc).__name__)
        return _stage_summary("FAILED", error=exc)
    LOGGER.info("[AUTO-SYNC] %s incremental sync completed", name)
    return _stage_summary("SUCCESS", result={**metrics(result), "result": result})


async def run_incremental_sync(*, master_url: str | None = None) -> dict:
    """Run master, response, and shortlist incremental syncs in order.

    Each stage is isolated so one failure does not prevent later stages. The
    called services retain ownership of their own checkpoints and idempotency.
    """
    started_at = datetime.now(timezone.utc)
    started_clock = monotonic()
    LOGGER.info("[AUTO-SYNC] Started")
    summary: dict[str, Any] = {
        "started_at": started_at,
        "completed_at": None,
        "duration": None,
        "master": None,
        "responses": {"status": "SUCCESS", "processed": 0, "skipped": 0, "failed_opportunities": []},
        "shortlist": {"status": "SUCCESS", "processed": 0, "skipped": 0, "failed_opportunities": [], "skipped_opportunities": []},
        "opportunity_results": [],
    }

    settings = get_settings()
    summary["master"] = await _run_stage(
        "Master",
        lambda: import_master_incremental_from_url(url=(master_url or settings.student_sheet_url or "").strip()),
        _master_metrics,
    )

    master_result = summary["master"].get("result") or {}
    processed_opportunities = master_result.get("processed_opportunities")
    if processed_opportunities is None:
        processed_opportunities = [
            {"opportunity_id": opportunity_id, "is_new": False}
            for opportunity_id in master_result.get("opportunity_ids", [])
        ]
    LOGGER.info("[INCREMENTAL] Last synced opportunity date: %s", master_result.get("latest_opportunity_date", "unknown"))
    LOGGER.info("[INCREMENTAL] Found %d Master opportunities to process", len(processed_opportunities))
    for processed in processed_opportunities:
        opportunity_id = processed["opportunity_id"]
        is_new = bool(processed.get("is_new"))
        label = f"{processed.get('company') or 'Unknown company'} / {processed.get('role') or 'Unknown role'}"
        opportunity_result = {
            "opportunity_id": opportunity_id,
            "is_new": is_new,
            "master": {"status": "created" if is_new else "updated"},
            "company": processed.get("company"),
            "role": processed.get("role"),
            "received_on": processed.get("received_on"),
        }
        LOGGER.info("[AUTO-SYNC] Master: %s -> %s", label, "CREATED" if is_new else "UPDATED")
        if processed.get("response_url_present") is False:
            opportunity_result["response"] = {"status": "SKIPPED", "reason": "No response sheet URL is stored on this opportunity."}
            opportunity_result["shortlist"] = {"status": "SKIPPED", "reason": "Skipped because response import was not performed."}
            summary["responses"]["skipped"] += 1
            summary["shortlist"]["skipped"] += 1
            summary["shortlist"]["skipped_opportunities"].append({"opportunity_id": opportunity_id, "reason": opportunity_result["shortlist"]["reason"]})
            summary["opportunity_results"].append(opportunity_result)
            LOGGER.info("[AUTO-SYNC] Response: %s -> SKIPPED: response sheet URL missing", label)
            LOGGER.info("[AUTO-SYNC] Shortlist: %s -> SKIPPED: response import was not performed", label)
            continue
        response_operation = (
            (lambda opportunity_id=opportunity_id: sync_from_sheet(
                opportunity_id=opportunity_id, kind="responses", confirm=True, force=True,
            ))
            if is_new
            else (lambda opportunity_id=opportunity_id: sync_response_sheet_incremental(opportunity_id=opportunity_id))
        )
        response_result = await _run_stage(f"Response {opportunity_id}", response_operation, _response_metrics)
        if response_result["status"] == "FAILED":
            response_error = response_result.get("error_detail") or response_result.get("error")
            if _missing_sheet(response_error, "response"):
                opportunity_result["response"] = {"status": "SKIPPED", "reason": "Response sheet URL missing"}
            else:
                summary["responses"]["failed_opportunities"].append(opportunity_id)
                opportunity_result["response"] = {"status": "FAILED", "error": response_error}
            opportunity_result["shortlist"] = {"status": "SKIPPED", "reason": "Response import failed; shortlist was not attempted."}
            summary["shortlist"]["skipped_opportunities"].append({"opportunity_id": opportunity_id, "reason": opportunity_result["shortlist"]["reason"]})
            summary["opportunity_results"].append(opportunity_result)
            LOGGER.info("[AUTO-SYNC] Response: %s -> %s", label, opportunity_result["response"].get("error") or opportunity_result["response"].get("reason"))
            LOGGER.info("[AUTO-SYNC] Shortlist: %s -> SKIPPED: response import failed", label)
            continue
        summary["responses"]["processed"] += response_result["processed"]
        summary["responses"]["skipped"] += response_result["skipped"]
        opportunity_result["response"] = {"status": "SUCCESS", "processed": response_result["processed"], "skipped": response_result["skipped"]}
        LOGGER.info("[AUTO-SYNC] Response: %s -> IMPORTED", label)

        if processed.get("shortlist_url_present") is False:
            opportunity_result["shortlist"] = {"status": "SKIPPED", "reason": "No shortlist sheet URL is stored on this opportunity."}
            summary["shortlist"]["skipped"] += 1
            summary["opportunity_results"].append(opportunity_result)
            LOGGER.info("[AUTO-SYNC] Shortlist: %s -> SKIPPED: shortlist sheet URL missing", label)
            continue

        shortlist_operation = (
            (lambda opportunity_id=opportunity_id: sync_from_sheet(
                opportunity_id=opportunity_id, kind="shortlist", confirm=True, force=True,
            ))
            if is_new
            else (lambda opportunity_id=opportunity_id: sync_shortlist_sheet_incremental(opportunity_id=opportunity_id))
        )
        shortlist_result = await _run_stage(f"Shortlist {opportunity_id}", shortlist_operation, _shortlist_metrics)
        if shortlist_result["status"] == "FAILED":
            shortlist_error = shortlist_result.get("error_detail") or shortlist_result.get("error")
            if _missing_sheet(shortlist_error, "company / shortlist"):
                opportunity_result["shortlist"] = {"status": "SKIPPED", "reason": "Shortlist sheet URL missing"}
            else:
                summary["shortlist"]["failed_opportunities"].append(opportunity_id)
                opportunity_result["shortlist"] = {"status": "FAILED", "error": shortlist_error}
            LOGGER.info("[AUTO-SYNC] Shortlist: %s -> %s", label, opportunity_result["shortlist"].get("error") or opportunity_result["shortlist"].get("reason"))
        else:
            summary["shortlist"]["processed"] += shortlist_result["processed"]
            summary["shortlist"]["skipped"] += shortlist_result["skipped"]
            opportunity_result["shortlist"] = {"status": "SUCCESS", "processed": shortlist_result["processed"], "skipped": shortlist_result["skipped"]}
            LOGGER.info("[AUTO-SYNC] Shortlist: %s -> IMPORTED", label)
        summary["opportunity_results"].append(opportunity_result)

    if summary["responses"]["failed_opportunities"]:
        summary["responses"]["status"] = "FAILED"
    if summary["shortlist"]["failed_opportunities"]:
        summary["shortlist"]["status"] = "FAILED"
    summary["completed_at"] = datetime.now(timezone.utc)
    summary["duration"] = round(monotonic() - started_clock, 3)
    failed = summary["master"]["status"] == "FAILED" or summary["responses"]["status"] == "FAILED" or summary["shortlist"]["status"] == "FAILED"
    summary["status"] = "FAILED" if failed else "SUCCESS"
    LOGGER.info("[AUTO-SYNC] Completed %s", "with failures" if failed else "successfully")
    return summary


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
