from pydantic import BaseModel


class ApplicationStatusUpdate(BaseModel):
    new_status: str
    reason: str | None = None
    notes: str | None = None
    changed_by: str | None = None
    changed_by_role: str = "admin"
    source: str = "manual"
