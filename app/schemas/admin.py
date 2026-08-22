from typing import Literal

from pydantic import BaseModel, Field


class OpportunityDeleteRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class StudentIssueStatusUpdate(BaseModel):
    status: Literal["IN_PROGRESS", "CLOSED"]


class MasterIncrementalRequest(BaseModel):
    url: str = Field(..., min_length=1, description="A public Google Sheets master tracker URL.")
