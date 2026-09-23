from typing import Literal

from pydantic import BaseModel, Field, model_validator


class OpportunityDeleteRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class StudentIssueStatusUpdate(BaseModel):
    """A status change on a student's ticket, with the admin's reply.

    Closing needs a reply: the student raised the ticket because something was
    wrong for them, and a ticket that just turns CLOSED tells them nothing.
    """

    status: Literal["IN_PROGRESS", "CLOSED"]
    response: str | None = Field(
        default=None,
        max_length=4000,
        description="The admin's answer, shown to the student. Required when closing.",
    )

    @model_validator(mode="after")
    def _closing_requires_an_answer(self) -> "StudentIssueStatusUpdate":
        if self.status == "CLOSED" and not (self.response or "").strip():
            raise ValueError("Write a response for the student before closing this ticket.")
        return self


class MasterIncrementalRequest(BaseModel):
    url: str = Field(..., min_length=1, description="A public Google Sheets master tracker URL.")


class MasterPasteRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, description="Master rows pasted as TSV/CSV; the header row is optional.")
    confirm: bool = Field(default=False, description="False previews without writing; true performs the import.")
    url: str | None = Field(default=None, description="Master sheet link, whose header is used when the paste has none.")
