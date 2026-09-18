from typing import Literal

from pydantic import BaseModel, Field


class OpportunityDeleteRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class StudentIssueStatusUpdate(BaseModel):
    status: Literal["IN_PROGRESS", "CLOSED"]


class MasterIncrementalRequest(BaseModel):
    url: str = Field(..., min_length=1, description="A public Google Sheets master tracker URL.")


class MasterPasteRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, description="Master rows pasted as TSV/CSV; the header row is optional.")
    confirm: bool = Field(default=False, description="False previews without writing; true performs the import.")
    url: str | None = Field(default=None, description="Master sheet link, whose header is used when the paste has none.")
