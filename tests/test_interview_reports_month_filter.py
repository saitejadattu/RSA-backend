from datetime import datetime, timezone
from bson import ObjectId
import pytest
from fastapi import HTTPException

from app.services import admin_dashboard_service, interview_report_service, transcript_service
from app.services.transcript_service import (
    extract_date_from_text,
    extract_header_date,
    parse_header,
    resolve_interview_date,
)
from app.services.admin_dashboard_service import (
    build_company_feedback_docx,
    list_admin_reports,
    group_reports_by_student_id,
)


class _FakeAggregateCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class _FakeReportsCollection:
    def __init__(self, raw_reports, sessions=None, transcripts=None, companies=None, students=None, opportunities=None):
        self.raw_reports = raw_reports
        self.sessions = {str(s["_id"]): s for s in (sessions or [])}
        self.transcripts = {str(t.get("session_id")): t for t in (transcripts or [])}
        self.companies = {str(c["_id"]): c for c in (companies or [])}
        self.students = {str(st["_id"]): st for st in (students or [])}
        self.opportunities = {str(op["_id"]): op for op in (opportunities or [])}

    def aggregate(self, pipeline):
        results = []
        for r in self.raw_reports:
            item = dict(r)
            sess = self.sessions.get(str(item.get("session_id")), {})
            tr = self.transcripts.get(str(item.get("session_id")), {})
            co = self.companies.get(str(item.get("company_id")), {})
            st = self.students.get(str(item.get("student_id")), {})
            opp = self.opportunities.get(str(item.get("opportunity_id")), {})

            for stage in pipeline:
                match = stage.get("$match")
                if match:
                    if "company_id" in match and item.get("company_id") != match["company_id"]:
                        item = None
                        break
                    if "visible_to_student" in match:
                        if match["visible_to_student"] is True and not item.get("visible_to_student"):
                            item = None
                            break
                        if match["visible_to_student"] == {"$ne": True} and item.get("visible_to_student"):
                            item = None
                            break
                if "$lookup" in stage:
                    lookup = stage["$lookup"]
                    local_field = lookup.get("localField")
                    foreign_collection = lookup.get("from")
                    if local_field and foreign_collection in {"interview_sessions", "transcripts", "students", "companies", "hiring_opportunities"}:
                        value = item.get(local_field)
                        if value is not None:
                            key = str(value)
                            if foreign_collection == "interview_sessions":
                                item[f"{lookup.get('as')}"] = self.sessions.get(key, {})
                            elif foreign_collection == "transcripts":
                                item[f"{lookup.get('as')}"] = self.transcripts.get(key, {})
                            elif foreign_collection == "students":
                                item[f"{lookup.get('as')}"] = self.students.get(key, {})
                            elif foreign_collection == "companies":
                                item[f"{lookup.get('as')}"] = self.companies.get(key, {})
                            elif foreign_collection == "hiring_opportunities":
                                item[f"{lookup.get('as')}"] = self.opportunities.get(key, {})

                if "$unwind" in stage:
                    unwind = stage["$unwind"]
                    path = unwind.get("path")
                    if path and isinstance(item.get(path[1:]), dict):
                        arr = item.get(path[1:])
                        if arr:
                            item[path[1:]] = arr
                        else:
                            item[path[1:]] = {}

            if item is None:
                continue

            projected = {
                "_id": item.get("_id"),
                "student": {"id": item.get("st", {}).get("_id") or st.get("_id"), "name": item.get("st", {}).get("name") or st.get("name"), "phone": item.get("st", {}).get("phone") or st.get("phone")},
                "student_id": item.get("student_id"),
                "company_id": item.get("company_id"),
                "company": item.get("co", {}).get("name") if isinstance(item.get("co"), dict) else co.get("name") if isinstance(co, dict) else co,
                "role": item.get("opp", {}).get("role") if isinstance(item.get("opp"), dict) else opp.get("role") if isinstance(opp, dict) else opp,
                "opportunity_id": item.get("opportunity_id"),
                "visible_to_student": item.get("visible_to_student", False),
                "interview_date": item.get("interview_date"),
                "generated_at": item.get("generated_at"),
                "created_at": item.get("created_at"),
                "overall": item.get("overall", {}),
                "answers": item.get("answers", []),
                "session": item.get("sess") or sess,
                "sess": item.get("sess") or sess,
                "transcript": item.get("tr") or tr,
                "tr": item.get("tr") or tr,
            }
            results.append(projected)

        return _FakeAggregateCursor(results)

    async def find_one(self, query, projection=None):
        for r in self.raw_reports:
            if all(r.get(k) == v for k, v in query.items()):
                return r
        return None


class _FakeCompaniesCollection:
    def __init__(self, companies):
        self.companies = {str(c["_id"]): c for c in companies}

    async def find_one(self, query, projection=None):
        target_id = str(query.get("_id"))
        return self.companies.get(target_id)


def test_extract_date_from_text_formats():
    """
    Test extraction of dates from titles, company hints, and text with various formats.
    """
    # 1. Title format: "NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript"
    t1 = "NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript"
    d1 = extract_date_from_text(t1)
    assert d1 == datetime(2026, 6, 30, 14, 56, tzinfo=timezone.utc)

    # 2. Title format: "NxtWave X Drip Off AI - 2026/07/29 15:57 IST - Transcript"
    t2 = "NxtWave X Drip Off AI - 2026/07/29 15:57 IST - Transcript"
    d2 = extract_date_from_text(t2)
    assert d2 == datetime(2026, 7, 29, 15, 57, tzinfo=timezone.utc)

    # 3. Company hint: "GTMER - 2026/06/30 14:56 IST"
    t3 = "GTMER - 2026/06/30 14:56 IST"
    d3 = extract_date_from_text(t3)
    assert d3 == datetime(2026, 6, 30, 14, 56, tzinfo=timezone.utc)

    # 4. Month name format: "July 29, 2026 15:57 IST"
    t4 = "July 29, 2026 15:57 IST"
    d4 = extract_date_from_text(t4)
    assert d4 == datetime(2026, 7, 29, 15, 57, tzinfo=timezone.utc)

    # 5. Dash format: "2026-07-29"
    t5 = "Interviews | Nxtwave X WeSee - 2026-07-29 - Transcript"
    d5 = extract_date_from_text(t5)
    assert d5 == datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def test_parse_header_embedded_title_and_cleaned_hint():
    """
    Test that parse_header extracts date from title and cleans company_hint.
    """
    raw_gtmer = """NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript
Suhas Pulapa: Good morning"""
    h_gtmer = parse_header(raw_gtmer)
    assert h_gtmer["meeting_date"] == datetime(2026, 6, 30, 14, 56, tzinfo=timezone.utc)
    assert h_gtmer["company_hint"] == "GTMER"

    raw_drip = """NxtWave X Drip Off AI - 2026/07/29 15:57 IST - Transcript
Interviewer: Hello candidate"""
    h_drip = parse_header(raw_drip)
    assert h_drip["meeting_date"] == datetime(2026, 7, 29, 15, 57, tzinfo=timezone.utc)
    assert h_drip["company_hint"] == "Drip Off AI"


@pytest.mark.asyncio
async def test_nine_tier_date_priority():
    """
    Test the strict 9-tier priority:
    1. interview_date
    2. scheduled_at
    3. started_at
    4. meeting_date
    5. title extracted date
    6. company_hint extracted date
    7. raw_text extracted date
    8. generated_at
    9. created_at
    """
    d1 = datetime(2026, 7, 1, tzinfo=timezone.utc)
    d2 = datetime(2026, 7, 2, tzinfo=timezone.utc)
    d3 = datetime(2026, 7, 3, tzinfo=timezone.utc)
    d4 = datetime(2026, 7, 4, tzinfo=timezone.utc)
    d8 = datetime(2026, 8, 1, tzinfo=timezone.utc)
    d9 = datetime(2026, 8, 2, tzinfo=timezone.utc)

    # 1. Tier 1 wins
    assert resolve_interview_date(
        report={"interview_date": d1, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": d2, "started_at": d3},
        transcript={"meeting_date": d4, "title": "2026/07/05"},
    ) == d1

    # 2. Tier 2 wins when Tier 1 is absent
    assert resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": d2, "started_at": d3},
        transcript={"meeting_date": d4, "title": "2026/07/05"},
    ) == d2

    # 3. Tier 3 wins when 1-2 absent
    assert resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": d3},
        transcript={"meeting_date": d4, "title": "2026/07/05"},
    ) == d3

    # 4. Tier 4 wins when 1-3 absent
    assert resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript={"meeting_date": d4, "title": "2026/07/05"},
    ) == d4

    # 5. Tier 5 (title) wins when 1-4 absent
    res5 = resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript={"meeting_date": None, "title": "NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript"},
    )
    assert res5 == datetime(2026, 6, 30, 14, 56, tzinfo=timezone.utc)

    # 6. Tier 6 (company_hint) wins when 1-5 absent
    res6 = resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript={"meeting_date": None, "title": None, "company_hint": "GTMER - 2026/06/30 14:56 IST"},
    )
    assert res6 == datetime(2026, 6, 30, 14, 56, tzinfo=timezone.utc)

    # 7. Tier 7 (raw_text) wins when 1-6 absent
    res7 = resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript={"meeting_date": None, "title": None, "company_hint": None, "raw_text": "NxtWave X Drip Off AI - 2026/07/29 15:57 IST - Transcript\n..."},
    )
    assert res7 == datetime(2026, 7, 29, 15, 57, tzinfo=timezone.utc)

    # 8. Tier 8 (generated_at) fallback when 1-7 absent
    assert resolve_interview_date(
        report={"interview_date": None, "generated_at": d8, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript={"meeting_date": None, "title": None, "company_hint": None, "raw_text": None},
    ) == d8

    # 9. Tier 9 (created_at) fallback when 1-8 absent
    assert resolve_interview_date(
        report={"interview_date": None, "generated_at": None, "created_at": d9},
        session={"scheduled_at": None, "started_at": None},
        transcript=None,
    ) == d9


@pytest.mark.asyncio
async def test_gtmer_and_drip_off_regression_cases(monkeypatch):
    """
    Test actual cases where meeting_date is null:
    - GTMER: title = "NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript", meeting_date = null
      => June 2026 (2026-06)
    - Drip Off AI: company_hint = "Drip Off AI - 2026/07/29 15:57 IST", meeting_date = null, generated_at = August 2, 2026
      => July 2026 (2026-07)
    """
    gtmer_co_id = ObjectId()
    drip_co_id = ObjectId()
    student_id = ObjectId()

    # GTMER session & report
    gtmer_sess_id = ObjectId()
    gtmer_rep_id = ObjectId()
    gtmer_session = {"_id": gtmer_sess_id, "company_id": gtmer_co_id, "scheduled_at": None, "started_at": None}
    gtmer_transcript = {
        "session_id": gtmer_sess_id,
        "meeting_date": None,
        "title": "NxtWave X GTMER - 2026/06/30 14:56 IST - Transcript",
        "company_hint": "GTMER - 2026/06/30 14:56 IST",
    }
    gtmer_report = {
        "_id": gtmer_rep_id,
        "session_id": gtmer_sess_id,
        "company_id": gtmer_co_id,
        "student_id": student_id,
        "interview_date": None,
        "generated_at": datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc),
        "visible_to_student": True,
    }

    # Drip Off AI session & report
    drip_sess_id = ObjectId()
    drip_rep_id = ObjectId()
    drip_session = {"_id": drip_sess_id, "company_id": drip_co_id, "scheduled_at": None, "started_at": None}
    drip_transcript = {
        "session_id": drip_sess_id,
        "meeting_date": None,
        "title": "NxtWave X Drip Off AI - 2026/07/29 15:57 IST - Transcript",
        "company_hint": "Drip Off AI - 2026/07/29 15:57 IST",
    }
    drip_report = {
        "_id": drip_rep_id,
        "session_id": drip_sess_id,
        "company_id": drip_co_id,
        "student_id": student_id,
        "interview_date": None,
        "generated_at": datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        "created_at": datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc),
        "visible_to_student": True,
    }

    fake_db = {
        "interview_reports": _FakeReportsCollection(
            [gtmer_report, drip_report],
            sessions=[gtmer_session, drip_session],
            transcripts=[gtmer_transcript, drip_transcript],
            companies=[{"_id": gtmer_co_id, "name": "GTMER"}, {"_id": drip_co_id, "name": "Drip Off AI"}],
            students=[{"_id": student_id, "name": "Suhas Pulapa"}],
        ),
        "companies": _FakeCompaniesCollection([
            {"_id": gtmer_co_id, "name": "GTMER"},
            {"_id": drip_co_id, "name": "Drip Off AI"},
        ]),
    }
    monkeypatch.setattr(admin_dashboard_service, "get_database", lambda: fake_db)

    # Verify list_admin_reports returns correctly resolved interview_dates
    reports = await list_admin_reports()
    assert len(reports) == 2
    by_id = {str(r["id"]): r for r in reports}

    # 1. GTMER resolves to June 30, 2026 (2026-06)
    assert by_id[str(gtmer_rep_id)]["interview_date"].startswith("2026-06-30")
    assert by_id[str(gtmer_rep_id)]["interview_date"][:7] == "2026-06"

    # 2. Drip Off AI resolves to July 29, 2026 (2026-07), NOT August
    assert by_id[str(drip_rep_id)]["interview_date"].startswith("2026-07-29")
    assert by_id[str(drip_rep_id)]["interview_date"][:7] == "2026-07"

    # 3. Verify DOCX download with month filter
    doc_bytes_gtmer, fn_gtmer = await build_company_feedback_docx(str(gtmer_co_id), month="2026-06")
    assert doc_bytes_gtmer is not None
    assert "GTMER" in fn_gtmer

    doc_bytes_drip, fn_drip = await build_company_feedback_docx(str(drip_co_id), month="2026-07")
    assert doc_bytes_drip is not None
    assert "Drip_Off_AI" in fn_drip

    # 4. Verify August 2026 download for Drip Off AI returns 404
    with pytest.raises(HTTPException) as exc_info:
        await build_company_feedback_docx(str(drip_co_id), month="2026-08")
    assert exc_info.value.status_code == 404


def test_company_view_unique_company_count():
    """
    Company View:
    July 2026:
      - Drip Off AI (6 candidates)
      - Totem Interactive (4 candidates)
      - Drip Off AI (1 candidate)
    => Total distinct companies in July = 2 (July 2026 (2)).
    """
    drip_off_id = str(ObjectId())
    totem_id = str(ObjectId())

    # Simulate 11 reports
    reports = []
    # 6 for Drip Off AI
    for _ in range(6):
        reports.append({
            "company_id": drip_off_id,
            "company": "Drip Off AI",
            "interview_date": datetime(2026, 7, 10, tzinfo=timezone.utc),
            "generated_at": datetime(2026, 8, 2, tzinfo=timezone.utc),
        })
    # 4 for Totem Interactive
    for _ in range(4):
        reports.append({
            "company_id": totem_id,
            "company": "Totem Interactive",
            "interview_date": datetime(2026, 7, 15, tzinfo=timezone.utc),
            "generated_at": datetime(2026, 8, 3, tzinfo=timezone.utc),
        })
    # 1 more for Drip Off AI
    reports.append({
        "company_id": drip_off_id,
        "company": "Drip Off AI",
        "interview_date": datetime(2026, 7, 20, tzinfo=timezone.utc),
        "generated_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
    })

    # Group by month and compute unique company count
    month_company_sets = {}
    for r in reports:
        dt = r["interview_date"]
        month_key = f"{dt.year}-{dt.month:02d}"
        month_company_sets.setdefault(month_key, set()).add(r["company_id"])

    assert len(month_company_sets["2026-07"]) == 2
    assert month_company_sets["2026-07"] == {drip_off_id, totem_id}


def test_student_view_grouping_by_student_id():
    """
    Student View:
    Grouping should be strictly by canonical student ID.
    """
    student_1_id = ObjectId()
    student_2_id = ObjectId()

    reports = [
        {
            "_id": ObjectId(),
            "student_id": student_1_id,
            "student": {"id": student_1_id, "name": "Alice"},
            "company": {"name": "Company A"},
        },
        {
            "_id": ObjectId(),
            "student_id": student_1_id,
            "student": {"id": student_1_id, "name": "Alice"},
            "company": {"name": "Company B"},
        },
        {
            "_id": ObjectId(),
            "student_id": student_2_id,
            "student": {"id": student_2_id, "name": "Bob"},
            "company": {"name": "Company A"},
        },
    ]

    grouped = group_reports_by_student_id(reports)
    assert len(grouped) == 2
    assert grouped[0]["student_id"] == str(student_1_id)
    assert len(grouped[0]["reports"]) == 2
    assert grouped[1]["student_id"] == str(student_2_id)
    assert len(grouped[1]["reports"]) == 1
