from fastapi import APIRouter, Depends, Query

from app.schemas.student import StudentCreate, StudentImportRequest, StudentImportResponse, StudentResponse
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
