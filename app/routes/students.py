from fastapi import APIRouter, Depends, Query

from app.schemas.student import StudentCreate, StudentImportRequest, StudentImportResponse, StudentResponse
from app.services.interview_report_service import (
    company_interview_insights,
    list_student_reports,
    student_practice_questions,
)
from app.services.student_dashboard_service import get_student_dashboard, list_student_applications
from app.services.student_service import create_student, import_students_from_sheet, list_students_for_debug
from app.utils.dependencies import get_current_student, require_admin_sync_token
from app.utils.object_id import serialize_document


router = APIRouter(prefix="/students", tags=["Students"])


@router.get("/me/dashboard")
async def get_my_dashboard(current_student: dict = Depends(get_current_student)) -> dict:
    return await get_student_dashboard(current_student)


@router.get("/me/applications")
async def get_my_applications(current_student: dict = Depends(get_current_student)) -> list[dict]:
    return await list_student_applications(current_student)


@router.get("/me/reports")
async def get_my_reports(current_student: dict = Depends(get_current_student)) -> list[dict]:
    """The student's own RSA interview reports. Only reports an admin has
    published (visible_to_student=True) are ever returned."""
    return await list_student_reports(current_student["_id"])


@router.get("/me/practice-questions")
async def get_practice_questions(
    include_scenario: bool = Query(default=False, description="Scenario questions are hidden unless asked for."),
    category: str | None = None,
    difficulty: str | None = None,
    search: str | None = None,
    limit: int = Query(default=300, ge=1, le=500),
    current_student: dict = Depends(get_current_student),
) -> dict:
    """Real technical questions asked across companies, for practice.

    Shared with every student, so it carries no personal data: no student names,
    no answers and no scores - only the question, topic and a model answer.
    """
    return await student_practice_questions(
        include_scenario=include_scenario,
        category=category,
        difficulty=difficulty,
        search=search,
        limit=limit,
    )


@router.get("/me/company-insights/{company_id}")
async def get_company_insights(
    company_id: str,
    current_student: dict = Depends(get_current_student),
) -> dict:
    """What this company tends to ask, so a student can prepare for them.

    Focus areas are derived from the company's own questions. Carries no
    personal data about who was interviewed or how they did.
    """
    return await company_interview_insights(company_id)


@router.get("/me", response_model=StudentResponse)
async def get_me(current_student: dict = Depends(get_current_student)) -> dict:
    return serialize_document(current_student)


@router.get("/dev-check", response_model=list[StudentResponse])
async def dev_check_students(
    identifier: str | None = Query(default=None, description="Optional phone number or email"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    return await list_students_for_debug(limit=limit, identifier=identifier)


@router.post("/", response_model=StudentResponse, dependencies=[Depends(require_admin_sync_token)])
async def add_student(payload: StudentCreate) -> dict:
    return await create_student(payload)


@router.post("/import-sheet", response_model=StudentImportResponse, dependencies=[Depends(require_admin_sync_token)])
async def import_sheet(payload: StudentImportRequest) -> dict:
    return await import_students_from_sheet(payload.sheet_url)
