from datetime import datetime, timezone
from bson import ObjectId
import pytest

from app.services.interview_report_service import _advance_after_interview, _record_status, FINAL_APPLICATION_STATUSES
from app.services.admin_company_service import bulk_reject_interviewed, _blank_counts, _tally
from app.services.student_dashboard_service import _apply_student_outcome
from app.services.status_history_service import update_application_status
from app.schemas.status_history import ApplicationStatusUpdate


class _FakeCursor:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        self._iter = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self.items)


class _FakeApplicationsCollection:
    def __init__(self, docs):
        self.docs = {doc["_id"]: doc for doc in docs}

    async def find_one(self, query, projection=None):
        for doc in self.docs.values():
            match = True
            for k, v in query.items():
                if k == "$or":
                    or_match = False
                    for or_clause in v:
                        sub_match = True
                        for ok, ov in or_clause.items():
                            if isinstance(ov, dict) and "$in" in ov:
                                if doc.get(ok) not in ov["$in"]:
                                    sub_match = False
                            elif doc.get(ok) != ov:
                                sub_match = False
                        if sub_match:
                            or_match = True
                            break
                    if not or_match:
                        match = False
                        break
                elif isinstance(v, dict):
                    if "$in" in v and doc.get(k) not in v["$in"]:
                        match = False
                        break
                    if "$nin" in v and doc.get(k) in v["$nin"]:
                        match = False
                        break
                    if "$ne" in v and doc.get(k) == v["$ne"]:
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                if projection:
                    res = {k: doc.get(k) for k in projection if k in doc}
                    if "_id" not in projection and projection.get("_id") != 0:
                        res["_id"] = doc["_id"]
                    return res
                return dict(doc)
        return None

    def find(self, query, projection=None):
        matches = []
        for doc in self.docs.values():
            match = True
            for k, v in query.items():
                if k == "$or":
                    or_match = False
                    for or_clause in v:
                        sub_match = True
                        for ok, ov in or_clause.items():
                            if isinstance(ov, dict) and "$in" in ov:
                                if doc.get(ok) not in ov["$in"]:
                                    sub_match = False
                            elif isinstance(ov, dict) and "$exists" in ov:
                                exists = ok in doc
                                if exists != ov["$exists"]:
                                    sub_match = False
                            elif doc.get(ok) != ov:
                                sub_match = False
                        if sub_match:
                            or_match = True
                            break
                    if not or_match:
                        match = False
                        break
                elif isinstance(v, dict):
                    if "$in" in v and doc.get(k) not in v["$in"]:
                        match = False
                        break
                    if "$nin" in v and doc.get(k) in v["$nin"]:
                        match = False
                        break
                    if "$ne" in v and doc.get(k) == v["$ne"]:
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                res = dict(doc)
                if projection:
                    res = {pk: doc.get(pk) for pk in projection if pk in doc}
                    if "_id" not in projection and projection.get("_id") != 0:
                        res["_id"] = doc["_id"]
                matches.append(res)
        return _FakeCursor(matches)

    async def update_one(self, query, update):
        for doc_id, doc in self.docs.items():
            if query.get("_id") == doc_id:
                if "$set" in update:
                    doc.update(update["$set"])
                return True
        return False


class _FakeSimpleCollection:
    def __init__(self, docs=None):
        self.docs = {doc["_id"]: doc for doc in (docs or [])}
        self.inserted = []

    async def find_one(self, query, projection=None):
        for doc in self.docs.values():
            match = True
            for k, v in query.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                return dict(doc)
        return None

    async def insert_one(self, doc):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = ObjectId()
        self.docs[d["_id"]] = d
        self.inserted.append(d)
        class Res:
            inserted_id = d["_id"]
        return Res()

    async def update_one(self, query, update):
        for doc_id, doc in self.docs.items():
            if query.get("_id") == doc_id:
                if "$set" in update:
                    doc.update(update["$set"])
                return True
        return False


class _FakeDB:
    def __init__(self, applications, opportunities=None, status_history=None):
        self.applications = _FakeApplicationsCollection(applications)
        self.opportunities = _FakeSimpleCollection(opportunities or [])
        self.status_history = _FakeSimpleCollection(status_history or [])

    def __getitem__(self, name):
        if name == "applications":
            return self.applications
        if name == "hiring_opportunities":
            return self.opportunities
        if name == "status_history":
            return self.status_history
        return _FakeSimpleCollection()


@pytest.mark.asyncio
async def test_scenario_a_and_b_transcript_advancement():
    """Scenario A: Shortlisted student without transcript remains SHORTLISTED (or marked not attended).
    Scenario B: Shortlisted student with transcript moves to INTERVIEW_COMPLETED.
    """
    now = datetime.now(timezone.utc)
    opp_id = ObjectId()
    student_a = ObjectId()
    student_b = ObjectId()

    app_a = {
        "_id": ObjectId(),
        "student_id": student_a,
        "opportunity_id": opp_id,
        "current_status": "SHORTLISTED",
    }
    app_b = {
        "_id": ObjectId(),
        "student_id": student_b,
        "opportunity_id": opp_id,
        "current_status": "SHORTLISTED",
    }

    db = _FakeDB([app_a, app_b], [{"_id": opp_id, "company_status": "Yet To Schedule Interviews"}])
    session = {"opportunity_id": opp_id}

    # Only Student B is in the transcript
    res = await _advance_after_interview(db, session=session, student_ids=[student_b], now=now)

    assert res["applications_advanced"] == 1
    assert db.applications.docs[app_b["_id"]]["current_status"] == "INTERVIEW_COMPLETED"
    # Student A was not in transcript: moved to INTERVIEW_NOT_ATTENDED, NOT rejected
    assert db.applications.docs[app_a["_id"]]["current_status"] == "INTERVIEW_NOT_ATTENDED"


@pytest.mark.asyncio
async def test_scenario_g_final_status_protection():
    """Scenario G: SELECTED / REJECTED candidates must NEVER be downgraded by transcript re-runs."""
    now = datetime.now(timezone.utc)
    opp_id = ObjectId()
    student_selected = ObjectId()
    student_rejected = ObjectId()

    app_sel = {
        "_id": ObjectId(),
        "student_id": student_selected,
        "opportunity_id": opp_id,
        "current_status": "SELECTED",
        "final_status": "HIRED",
    }
    app_rej = {
        "_id": ObjectId(),
        "student_id": student_rejected,
        "opportunity_id": opp_id,
        "current_status": "REJECTED",
        "final_status": "REJECTED",
    }

    db = _FakeDB([app_sel, app_rej], [{"_id": opp_id, "company_status": "Hiring-in-progress"}])
    session = {"opportunity_id": opp_id}

    # Re-running analysis for both students
    res = await _advance_after_interview(db, session=session, student_ids=[student_selected, student_rejected], now=now)

    # 0 advanced because both have final status
    assert res["applications_advanced"] == 0
    assert db.applications.docs[app_sel["_id"]]["current_status"] == "SELECTED"
    assert db.applications.docs[app_rej["_id"]]["current_status"] == "REJECTED"


@pytest.mark.asyncio
async def test_scenario_d_e_f_bulk_rejection():
    """Scenario D: INTERVIEW_COMPLETED candidates are marked REJECTED by bulk action.
    Scenario E: SHORTLISTED candidates without interview are NOT affected.
    Scenario F: Only exact opportunity_id is affected; other opportunities remain untouched.
    """
    now = datetime.now(timezone.utc)
    opp_1 = ObjectId()
    opp_2 = ObjectId()
    student_1 = ObjectId()
    student_2 = ObjectId()
    student_3 = ObjectId()

    # Opp 1: Student 1 has interview done, Student 2 is shortlisted only, Student 3 is already selected
    app_opp1_interviewed = {
        "_id": ObjectId(),
        "student_id": student_1,
        "opportunity_id": opp_1,
        "current_status": "INTERVIEW_COMPLETED",
        "final_status": None,
    }
    app_opp1_shortlisted = {
        "_id": ObjectId(),
        "student_id": student_2,
        "opportunity_id": opp_1,
        "current_status": "SHORTLISTED",
        "final_status": None,
    }
    app_opp1_selected = {
        "_id": ObjectId(),
        "student_id": student_3,
        "opportunity_id": opp_1,
        "current_status": "SELECTED",
        "final_status": "HIRED",
    }

    # Opp 2: Student 1 also applied here and has interview completed
    app_opp2_interviewed = {
        "_id": ObjectId(),
        "student_id": student_1,
        "opportunity_id": opp_2,
        "current_status": "INTERVIEW_COMPLETED",
        "final_status": None,
    }

    db = _FakeDB(
        [app_opp1_interviewed, app_opp1_shortlisted, app_opp1_selected, app_opp2_interviewed],
        [{"_id": opp_1}, {"_id": opp_2}],
    )

    from unittest.mock import patch
    with patch("app.services.admin_company_service.get_database", return_value=db):
        res = await bulk_reject_interviewed(str(opp_1))

    assert res["marked_not_selected"] == 1
    # Opp 1 interviewed -> REJECTED
    assert db.applications.docs[app_opp1_interviewed["_id"]]["current_status"] == "REJECTED"
    assert db.applications.docs[app_opp1_interviewed["_id"]]["final_status"] == "REJECTED"

    # Opp 1 shortlisted -> untouched!
    assert db.applications.docs[app_opp1_shortlisted["_id"]]["current_status"] == "SHORTLISTED"

    # Opp 1 selected -> untouched!
    assert db.applications.docs[app_opp1_selected["_id"]]["current_status"] == "SELECTED"

    # Opp 2 (different opportunity) -> untouched!
    assert db.applications.docs[app_opp2_interviewed["_id"]]["current_status"] == "INTERVIEW_COMPLETED"


def test_tally_and_blank_counts():
    """Verify _blank_counts and _tally correctly count interview_completed."""
    counts = _blank_counts()
    assert "interview_completed_count" in counts

    app_interview = {"current_status": "INTERVIEW_COMPLETED", "application_details": {"interested": True}}
    _tally(counts, app_interview)
    assert counts["interview_completed_count"] == 1
    assert counts["applied_count"] == 1


def test_applied_no_student_eligble_uses_default_remark_without_status_change():
    application = {
        "status": "APPLIED",
        "_remark": None,
        "opportunity": {"student_side_status": "No Student Eligble"},
    }

    _apply_student_outcome(application)

    assert application["student_outcome"] == "not_shortlisted"
    assert application["screening_remark"] == "Profile does not align with internship requirements"
    assert application["status"] == "APPLIED"

def test_applied_no_student_eligble_preserves_existing_remark_and_status():
    application = {
        "status": "APPLIED",
        "_remark": "Role requires a different skill set",
        "opportunity": {"student_side_status": "No Student Eligble"},
    }
    _apply_student_outcome(application)

    assert application["student_outcome"] == "not_shortlisted"
    assert application["screening_remark"] == "Role requires a different skill set"
    assert application["status"] == "APPLIED"


def test_applied_shared_profiles_with_crm_keeps_remark_empty_without_status_change():
    application = {
        "status": "APPLIED",
        "_remark": None,
        "opportunity": {"student_side_status": "Shared Profiles with CRM"},
    }

    _apply_student_outcome(application)

    assert application["student_outcome"] == "pending"
    assert application["screening_remark"] is None
    assert application["status"] == "APPLIED"

def test_applied_shared_profiles_with_crm_preserves_existing_remark_and_status():
    application = {
        "status": "APPLIED",
        "_remark": "Company provided feedback",
        "opportunity": {"student_side_status": "Shared Profiles with CRM"},
    }
    _apply_student_outcome(application)

    assert application["student_outcome"] == "pending"
    assert application["screening_remark"] == "Company provided feedback"
    assert application["status"] == "APPLIED"


@pytest.mark.parametrize("status", ["NOT_SHORTLISTED", "SHORTLISTED", "INTERVIEW_COMPLETED", "REJECTED", "SELECTED", "DROPPED"])
def test_existing_non_applied_student_outcomes_remain_unchanged(status):
    application = {"status": status, "opportunity": {"student_side_status": "No Student Eligble"}}

    _apply_student_outcome(application)

    expected = {
        "NOT_SHORTLISTED": "not_shortlisted",
        "SHORTLISTED": "shortlisted",
        "INTERVIEW_COMPLETED": "interview_done",
        "REJECTED": "rejected",
        "SELECTED": "selected",
        "DROPPED": "declined",
    }
    assert application["student_outcome"] == expected[status]
