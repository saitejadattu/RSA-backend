from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.schemas.student import StudentCreate, StudentImportRequest, StudentImportResponse, StudentIssueCreate, StudentResponse
from app.services.interview_report_service import (
    company_interview_insights,
    list_student_reports,
    student_practice_questions,
)
from app.services.student_dashboard_service import get_student_dashboard, list_student_applications
from app.services.student_issue_service import create_student_issue, get_student_issue, list_student_issues, reopen_student_issue
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


@router.post("/me/issues")
async def create_my_issue(
    payload: StudentIssueCreate,
    current_student: dict = Depends(get_current_student),
) -> dict:
    return await create_student_issue(current_student, payload)


@router.get("/me/issues")
async def get_my_issues(current_student: dict = Depends(get_current_student)) -> list[dict]:
    return await list_student_issues(current_student)


@router.get("/me/issues/{issue_id}")
async def get_my_issue(issue_id: str, current_student: dict = Depends(get_current_student)) -> dict:
    try:
        issue = await get_student_issue(current_student, issue_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


@router.post("/me/issues/{issue_id}/reopen")
async def reopen_my_issue(issue_id: str, current_student: dict = Depends(get_current_student)) -> dict:
    try:
        issue = await reopen_student_issue(current_student, issue_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if not issue:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


@router.get("/me/reports")
async def get_my_reports(current_student: dict = Depends(get_current_student)) -> list[dict]:
    """The student's own RSA interview reports. Only reports an admin has
    published (visible_to_student=True) are ever returned."""
    return await list_student_reports(current_student["_id"])


@router.get("/me/practice-questions")
async def get_practice_questions(
    include_scenario: bool = Query(default=False, description="Scenario questions are hidden unless asked for."),
    category: str | None = None,
    company: str | None = None,
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
        company=company,
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
