from fastapi import APIRouter

from app.schemas.status_history import ApplicationStatusUpdate
from app.services.status_history_service import list_status_history, update_application_status


router = APIRouter(prefix="/applications", tags=["Applications"])


@router.post("/{application_id}/status")
async def change_application_status(application_id: str, payload: ApplicationStatusUpdate) -> dict:
    return await update_application_status(application_id, payload)


@router.get("/{application_id}/status-history")
async def get_application_status_history(application_id: str) -> list[dict]:
    return await list_status_history(application_id)
