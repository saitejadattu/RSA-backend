from pydantic import BaseModel, Field


class TranscriptTextUpload(BaseModel):
    raw_text: str = Field(..., min_length=1, description="Full speaker-separated transcript text.")
    source: str = Field(default="paste", description="paste | upload | google_doc")


class SpeakerMapEntry(BaseModel):
    speaker_label: str
    student_id: str | None = None
    role: str = "unknown"


class SpeakerMapUpdate(BaseModel):
    speaker_map: list[SpeakerMapEntry] = Field(default_factory=list)


class ReportVisibilityUpdate(BaseModel):
    visible_to_student: bool = True


class TranscriptProposeRequest(BaseModel):
    raw_text: str = Field(..., min_length=1, description="Full pasted Google Meet transcript, header included.")
    opportunity_id: str | None = Field(
        default=None,
        description="Resolve an ambiguous opening (company posted several roles the same day). "
        "Re-propose with this to map speakers against that opening's shortlist.",
    )


class SheetPasteRequest(BaseModel):
    """A pasted response/shortlist sheet. confirm=false only previews."""

    raw_text: str = Field(..., min_length=1, description="Sheet contents pasted as TSV or CSV, header row included.")
    confirm: bool = Field(default=False, description="False previews without writing; true performs the import.")
    replace: bool = Field(
        default=False,
        description="Remove response-sourced candidates not in this sheet (safe ones deleted, advanced ones flagged).",
    )


class SheetSyncRequest(BaseModel):
    """Fetch the opening's stored sheet URL and import it. confirm=false previews."""

    confirm: bool = Field(default=False, description="False previews without writing; true performs the import.")
    force: bool = Field(default=False, description="Re-import even if this opening was already extracted.")
    replace: bool = Field(
        default=False,
        description="Remove response-sourced candidates not in this sheet (safe ones deleted, advanced ones flagged).",
    )


class SheetUrlRequest(BaseModel):
    """Fetch a sheet by URL and import it. confirm=false previews."""

    url: str = Field(..., min_length=1, description="A public ('anyone with the link') Google Sheets URL.")
    confirm: bool = Field(default=False, description="False previews without writing; true performs the import.")


class TranscriptConfirmRequest(BaseModel):
    """The admin-reviewed proposal. Nothing is written until this is posted."""

    raw_text: str = Field(..., min_length=1)
    company_id: str
    opportunity_id: str
    speaker_map: list[SpeakerMapEntry] = Field(default_factory=list)
    round_name: str = "Interview"
    round_type: str | None = None
    source: str = "paste"
