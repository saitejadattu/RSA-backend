from fastapi import APIRouter, Depends, Query

from app.schemas.interview_report import ReportVisibilityUpdate
from app.services.admin_company_service import get_admin_company_detail, get_admin_opportunity_detail
from app.services.admin_dashboard_service import (
    get_admin_analytics,
    get_admin_dashboard,
    list_admin_students,
    list_recent_applications,
)
from app.services.interview_report_service import list_questions, question_bank, set_report_visibility
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


@router.get("/companies/{company_id}")
async def company_detail(company_id: str) -> dict:
    return await get_admin_company_detail(company_id)


@router.get("/opportunities/{opportunity_id}")
async def opportunity_detail(opportunity_id: str) -> dict:
    return await get_admin_opportunity_detail(opportunity_id)


@router.get("/analytics")
async def analytics() -> dict:
    return await get_admin_analytics()


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


@router.patch("/reports/{report_id}/visibility")
async def report_visibility(report_id: str, payload: ReportVisibilityUpdate) -> dict:
    """Publish (or unpublish) an RSA report to the student."""
    return await set_report_visibility(report_id, payload.visible_to_student)
