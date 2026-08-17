from datetime import datetime, timezone

import pytest

from app.services.admin_dashboard_service import (
    _add_student_feedback_section,
    _export_filename_for_scope,
    _new_feedback_document,
    _sanitize_filename,
    group_reports_by_student_id,
    resolve_student_export_scope,
)


def test_student_export_docx_structure_omits_marker_fields():
    report = {
        "student": {"name": "Suhas Pulapa"},
        "company": {"name": "Drip Off AI"},
        "opportunity": {"role": "Software Engineer Intern"},
        "session": {"scheduled_at": datetime(2026, 8, 6, tzinfo=timezone.utc)},
        "visible_to_student": True,
        "overall": {"score": 6, "summary": "Strong overall performance."},
        "interviewer_satisfaction": "Met the bar with depth.",
        "answers": [{
            "question_text": "Tell me about yourself.",
            "student_answer": "I built a product team.",
            "accuracy": 16,
            "ideal_answer": "Explain background and impact.",
            "feedback": "Clear and direct.",
        }],
        "strengths": ["Clear communication"],
        "improvements": ["Need more depth in system design"],
        "communication": {"notes": "Good communication and confidence."},
        "skill_ratings": {"python": 4},
        "coaching_note": "Practice mock interviews.",
    }

    document = _new_feedback_document("Student Interview Feedback")
    _add_student_feedback_section(document, report, 1)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)

    assert "Student 1" not in text
    assert "Status" not in text
    assert "Pending" not in text and "Published" not in text
    assert "COACHING FOR NEXT TIME" not in text
    assert "End of Student" not in text
    assert "student interview feedback" in text.lower()
    assert "overall feedback" in text.lower()
    assert "how they met the bar" in text.lower()
    assert "questions & answers" in text.lower()


def test_student_scope_uses_canonical_student_id_and_filters_history():
    reports = [
        {"_id": "r1", "student_id": "student-123", "company": {"name": "Drip Off AI"}},
        {"_id": "r2", "student_id": "student-123", "company": {"name": "Totem Interactive"}},
        {"_id": "r3", "student_id": "student-456", "company": {"name": "Other Company"}},
    ]

    resolved = resolve_student_export_scope(reports, scope="student", student_id="student-123")

    assert resolved["student_id"] == "student-123"
    assert [report["_id"] for report in resolved["reports"]] == ["r1", "r2"]


def test_student_scope_rejects_missing_student_id():
    reports = [{"_id": "r1", "student_id": None, "student": {"name": "Suhas Pulapa"}}]

    with pytest.raises(ValueError, match="student_id"):
        resolve_student_export_scope(reports, scope="student", student_id=None)


def test_single_scope_keeps_only_selected_report():
    reports = [
        {"_id": "r1", "student_id": "student-123", "company": {"name": "Drip Off AI"}},
        {"_id": "r2", "student_id": "student-123", "company": {"name": "Totem Interactive"}},
    ]

    resolved = resolve_student_export_scope(reports, scope="single", student_id="student-123")

    assert resolved["student_id"] == "student-123"
    assert [report["_id"] for report in resolved["reports"]] == ["r1"]


def test_group_reports_by_student_id_counts_unique_companies_and_reports():
    reports = [
        {"_id": "r1", "student_id": "student-123", "student": {"name": "Suhas Pulapa"}, "company_id": "company-1", "company": {"name": "Drip Off AI"}},
        {"_id": "r2", "student_id": "student-123", "student": {"name": "Suhas Pulapa"}, "company_id": "company-1", "company": {"name": "Drip Off AI"}},
        {"_id": "r3", "student_id": "student-123", "student": {"name": "Suhas Pulapa"}, "company_id": "company-2", "company": {"name": "Totem Interactive"}},
        {"_id": "r4", "student_id": "student-456", "student": {"name": "Mahesh Kumar"}, "company_id": "company-3", "company": {"name": "LocalButcher"}},
    ]

    grouped = group_reports_by_student_id(reports)

    assert len(grouped) == 2
    suhas = next(group for group in grouped if group["student_id"] == "student-123")
    assert suhas["student_name"] == "Suhas Pulapa"
    assert suhas["company_count"] == 2
    assert suhas["interview_count"] == 3
    assert [report["_id"] for report in suhas["reports"]] == ["r1", "r2", "r3"]


def test_sanitize_filename_removes_unsafe_chars():
    assert _sanitize_filename("Suhas Pulapa") == "Suhas_Pulapa"
    assert _sanitize_filename("Drip Off AI") == "Drip_Off_AI"
    assert _sanitize_filename("Suhas-Pulapa") == "Suhas_Pulapa"
    assert _sanitize_filename("Thakkuri Shiva Kumar") == "Thakkuri_Shiva_Kumar"


def test_export_filename_for_scope_single_interview():
    """Single company/interview should include the company name."""
    filename = _export_filename_for_scope(
        "Suhas Pulapa",
        ["Drip Off AI"],
        scope="single",
        total_student_companies=4,
    )
    assert filename == "Suhas_Pulapa_Drip_Off_AI_Interview_Feedback.docx"


def test_export_filename_for_scope_all_companies():
    """All companies for student should NOT include company names."""
    filename = _export_filename_for_scope(
        "Suhas Pulapa",
        ["Drip Off AI", "Totem Interactive", "LocalButcher", "GTM / Gen AI"],
        scope="student",
        total_student_companies=4,
    )
    assert filename == "Suhas_Pulapa_Interview_Feedback.docx"


def test_export_filename_for_scope_selected_companies():
    """Selected subset of companies (not all) should include 'Selected_Companies'."""
    filename = _export_filename_for_scope(
        "Suhas Pulapa",
        ["Drip Off AI", "LocalButcher"],
        scope="selected",
        total_student_companies=4,
    )
    assert filename == "Suhas_Pulapa_Selected_Companies_Interview_Feedback.docx"


def test_export_filename_for_scope_selected_that_equals_all():
    """Selected scope that happens to equal all companies (edge case)."""
    filename = _export_filename_for_scope(
        "Suhas Pulapa",
        ["Drip Off AI"],
        scope="selected",
        total_student_companies=1,
    )
    assert filename == "Suhas_Pulapa_Interview_Feedback.docx"

