from fastapi import APIRouter, Depends, Query

from app.services.admin_dashboard_service import get_admin_dashboard, list_admin_students, list_recent_applications
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
