from fastapi import APIRouter, Depends, Query

from app.schemas.interview_report import (
    ReportVisibilityUpdate,
    SheetPasteRequest,
    SheetSyncRequest,
    SheetUrlRequest,
)
from app.services.admin_company_service import get_admin_company_detail, get_admin_opportunity_detail
from app.services.admin_dashboard_service import (
    get_admin_analytics,
    get_admin_dashboard,
    get_admin_student_detail,
    list_admin_reports,
    list_admin_students,
    list_pending_sessions,
    list_recent_applications,
)
from app.services.interview_report_service import list_questions, question_bank, set_report_visibility
from app.services.sheet_import_service import (
    import_master,
    import_master_from_url,
    import_responses,
    import_shortlist,
    sync_from_sheet,
)
from app.utils.dependencies import require_admin_access


router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(require_admin_access)])


@router.get("/dashboard")
async def dashboard() -> dict:
    return await get_admin_dashboard()


@router.get("/applications")
async def applications(
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict]:
    return await list_recent_applications(limit=limit, status_value=status)


@router.get("/students")
async def students(limit: int = Query(default=500, ge=1, le=1000)) -> list[dict]:
    return await list_admin_students(limit=limit)


@router.get("/students/{student_id}")
async def student_detail(student_id: str) -> dict:
    """Full profile for one student: info, pipeline stats, applications, role mix, reports."""
    return await get_admin_student_detail(student_id)


@router.get("/companies/{company_id}")
async def company_detail(company_id: str) -> dict:
    return await get_admin_company_detail(company_id)


@router.get("/opportunities/{opportunity_id}")
async def opportunity_detail(opportunity_id: str) -> dict:
    return await get_admin_opportunity_detail(opportunity_id)


@router.get("/analytics")
async def analytics(start: str | None = None, end: str | None = None) -> dict:
    """Monthly / custom-range analytics. start & end are YYYY-MM-DD (inclusive);
    defaults to the current month."""
    return await get_admin_analytics(start=start, end=end)


@router.get("/questions")
async def questions(
    company_id: str | None = None,
    opportunity_id: str | None = None,
    session_id: str | None = None,
    category: str | None = None,
    technical_only: bool = True,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    """Questions asked, filterable by company/opportunity/session for the company detail view."""
    return await list_questions(
        company_id=company_id,
        opportunity_id=opportunity_id,
        session_id=session_id,
        category=category,
        technical_only=technical_only,
        limit=limit,
    )


@router.get("/question-bank")
async def bank(
    technical_only: bool = True,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[dict]:
    """Deduplicated question bank: one row per distinct question with how often
    it was asked and which companies asked it."""
    return await question_bank(technical_only=technical_only, limit=limit)


@router.get("/sessions/pending")
async def pending_sessions() -> list[dict]:
    """Sessions with a transcript but no full RSA yet — drives the
    'Generate reports' button (analysed one at a time by the client)."""
    return await list_pending_sessions()


@router.get("/reports")
async def reports_list(published: bool | None = None) -> list[dict]:
    """All interview reports (newest first) with student/company/role and the full
    report body. published=true -> shared only, published=false -> pending only."""
    return await list_admin_reports(published=published)


@router.patch("/reports/{report_id}/visibility")
async def report_visibility(report_id: str, payload: ReportVisibilityUpdate) -> dict:
    """Publish (or unpublish) an RSA report to the student."""
    return await set_report_visibility(report_id, payload.visible_to_student)


@router.post("/companies/import")
async def import_company_master_sheet(payload: SheetPasteRequest) -> dict:
    """Paste rows from the company master tracker to create companies and their
    openings. Send confirm=false first to preview what would change.
    """
    return await import_master(raw_text=payload.raw_text, confirm=payload.confirm)


@router.post("/companies/import/fetch")
async def fetch_company_master_sheet(payload: SheetUrlRequest) -> dict:
    """Fetch the master tracker sheet from a public URL and import it.
    confirm=false previews. The sheet must be shared 'anyone with the link'.
    """
    return await import_master_from_url(url=payload.url, confirm=payload.confirm)


@router.post("/opportunities/{opportunity_id}/import/responses")
async def import_response_sheet(opportunity_id: str, payload: SheetPasteRequest) -> dict:
    """Paste a response sheet for this opening.

    Manual path for sheets that could not be downloaded. Send confirm=false
    first to see exactly what would change before anything is written.
    """
    return await import_responses(
        opportunity_id=opportunity_id, raw_text=payload.raw_text,
        confirm=payload.confirm, replace=payload.replace,
    )


@router.post("/opportunities/{opportunity_id}/import/shortlist")
async def import_shortlist_sheet(opportunity_id: str, payload: SheetPasteRequest) -> dict:
    """Paste a shortlist sheet for this opening. confirm=false previews only."""
    return await import_shortlist(
        opportunity_id=opportunity_id, raw_text=payload.raw_text, confirm=payload.confirm
    )


@router.post("/opportunities/{opportunity_id}/sync/{kind}")
async def sync_opportunity_sheet(opportunity_id: str, kind: str, payload: SheetSyncRequest) -> dict:
    """Fetch this opening's stored Google Sheet and import it. kind is
    'responses' or 'shortlist'. confirm=false previews without writing.
    Already-extracted openings are skipped unless force=true.
    """
    return await sync_from_sheet(
        opportunity_id=opportunity_id, kind=kind,
        confirm=payload.confirm, force=payload.force, replace=payload.replace,
    )
