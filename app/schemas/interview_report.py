from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class StudentFeedbackExportRequest(BaseModel):
    """Existing report ids and the requested student-feedback download format."""

    report_ids: list[str] = Field(default_factory=list, min_length=0, max_length=500)
    student_ids: list[str] = Field(default_factory=list, min_length=0, max_length=500, description="Canonical student IDs for multi-student export selections.")
    mode: str = Field(..., pattern="^(combined|separate|both)$")
    scope: str = Field(default="selected", pattern="^(selected|single|student)$")
    student_id: str | None = Field(default=None, description="Canonical student ID used to resolve the full student history.")


class CompanyFeedbackExportRequest(BaseModel):
    """Filtered report ids and the requested company-feedback download format."""

    report_ids: list[str] = Field(..., min_length=1, max_length=500)
    mode: str = Field(..., pattern="^(combined|separate|both)$")


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


class SheetLinksUpdate(BaseModel):
    """Set / correct the response and/or shortlist sheet URL on one opening.

    Omit a field to leave it untouched; send "" to clear it. Saving stamps the
    link as changed so the next Sync pulls the corrected sheet.
    """

    student_response_sheet: str | None = Field(default=None, description="Google Sheets URL for the response sheet.")
    company_sheet: str | None = Field(default=None, description="Google Sheets URL for the shortlist / company sheet.")


class TranscriptConfirmRequest(BaseModel):
    """The admin-reviewed proposal. Nothing is written until this is posted."""

    raw_text: str = Field(..., min_length=1)
    company_id: str
    opportunity_id: str
    speaker_map: list[SpeakerMapEntry] = Field(default_factory=list)
    round_name: str = "Interview"
    round_type: str | None = None
    source: str = "paste"


class ManualReportAnswer(BaseModel):
    model_config = {"extra": "forbid"}

    question_id: str | None = None
    question_text: str = Field(..., min_length=1)
    student_answer: str | None = None
    accuracy: float = Field(default=0, ge=0, le=100)
    correctness: Literal["correct", "partial", "incorrect", "not_answered"] = "not_answered"
    feedback: str | None = None
    ideal_answer: str | None = None


class ManualReportOverall(BaseModel):
    model_config = {"extra": "forbid"}

    score: float = Field(default=0, ge=0, le=10)
    verdict: Literal["strong", "average", "weak"] = "average"
    summary: str | None = None


class ManualReportCommunication(BaseModel):
    model_config = {"extra": "forbid"}

    clarity: float = Field(default=0, ge=0, le=5)
    confidence: float = Field(default=0, ge=0, le=5)
    notes: str | None = None


class ManualReportImprovement(BaseModel):
    model_config = {"extra": "forbid"}

    area: str = Field(..., min_length=1)
    detail: str = Field(..., min_length=1)
    priority: Literal["high", "medium", "low"] = "medium"


class ManualCandidateReport(BaseModel):
    model_config = {"extra": "forbid"}

    overall: ManualReportOverall = Field(default_factory=ManualReportOverall)
    answers: list[ManualReportAnswer] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    improvements: list[ManualReportImprovement] = Field(default_factory=list)
    skill_ratings: dict[str, float] = Field(default_factory=dict)
    communication: ManualReportCommunication = Field(default_factory=ManualReportCommunication)
    interviewer_feedback: str | None = None
    interviewer_satisfaction: str | None = None
    coaching_note: str | None = None


class ManualCandidateAnalysis(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_name: str | None = Field(default=None, min_length=1)
    speaker_label: str | None = Field(default=None, min_length=1)
    report: ManualCandidateReport


class ManualQuestion(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_name: str | None = Field(default=None, min_length=1)
    speaker_label: str | None = Field(default=None, min_length=1)
    question_text: str = Field(..., min_length=1)
    raw_question_text: str | None = None
    category: str = "other"
    topic: str | None = None
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    is_technical: bool = False
    question_type: str = "other"
    is_reusable: bool = False
    model_answer: str | None = None
    why_asked: str | None = None
    prepare: list[str] | str = Field(default_factory=list)

    @field_validator("prepare")
    @classmethod
    def validate_prepare(cls, value: list[str] | str) -> list[str]:
        items = [value] if isinstance(value, str) else value
        if len(items) > 5:
            raise ValueError("prepare must contain at most 5 items")
        return items


class ManualCompanyExpectations(BaseModel):
    model_config = {"extra": "forbid"}

    expectations: str = ""
    focus: list[str] = Field(default_factory=list)


class ManualAnalysisRequest(BaseModel):
    model_config = {"extra": "forbid"}

    candidates: list[ManualCandidateAnalysis] = Field(..., min_length=1)
    questions: list[ManualQuestion] = Field(default_factory=list)
    company_expectations: ManualCompanyExpectations | None = None
