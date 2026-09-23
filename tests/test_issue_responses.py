"""Closing a student's ticket requires an answer, and the student gets it.

A ticket that silently flips to CLOSED tells the student nothing about what was
wrong or what was done, so they reopen it or raise it again.
"""
import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.admin import StudentIssueStatusUpdate
from app.services import admin_issue_service
from app.services.student_issue_service import _student_issue

ISSUE_ID = ObjectId()
ADMIN = {"sub": "admin-1", "name": "Priya", "email": "priya@example.com"}


# ---- the rule ----

def test_closing_without_a_response_is_rejected():
    with pytest.raises(ValidationError) as error:
        StudentIssueStatusUpdate(status="CLOSED")

    assert "response" in str(error.value).lower()


def test_whitespace_is_not_a_response():
    with pytest.raises(ValidationError):
        StudentIssueStatusUpdate(status="CLOSED", response="   \n  ")


def test_closing_with_a_response_is_accepted():
    payload = StudentIssueStatusUpdate(status="CLOSED", response="Your applications are showing now.")

    assert payload.response.startswith("Your applications")


def test_moving_back_to_in_progress_needs_no_response():
    assert StudentIssueStatusUpdate(status="IN_PROGRESS").response is None


# ---- the write ----

class Issues:
    def __init__(self):
        self.doc = {"_id": ISSUE_ID, "status": "IN_PROGRESS", "title": "No clearance"}
        self.updates = []

    async def find_one(self, query, projection=None):
        return dict(self.doc) if query.get("_id") == ISSUE_ID else None

    async def update_one(self, query, update):
        self.updates.append(update)
        self.doc.update(update.get("$set", {}))
        for field, value in (update.get("$push") or {}).items():
            self.doc.setdefault(field, []).append(value)


class DB:
    def __init__(self, issues):
        self.issues = issues

    def __getitem__(self, name):
        return self.issues


@pytest.fixture
def issues(monkeypatch):
    collection = Issues()
    monkeypatch.setattr(admin_issue_service, "get_database", lambda: DB(collection))

    async def fake_get(issue_id):
        return dict(collection.doc)

    monkeypatch.setattr(admin_issue_service, "get_admin_issue", fake_get)
    return collection


@pytest.mark.asyncio
async def test_the_service_refuses_a_silent_close(issues):
    """The schema guards the route; this guards every other caller."""
    with pytest.raises(HTTPException) as error:
        await admin_issue_service.update_admin_issue_status(str(ISSUE_ID), "CLOSED", ADMIN, response="  ")

    assert error.value.status_code == 422
    assert issues.updates == []


@pytest.mark.asyncio
async def test_closing_stores_the_reply_and_keeps_a_history(issues):
    await admin_issue_service.update_admin_issue_status(
        str(ISSUE_ID), "CLOSED", ADMIN, response="Fixed — your 53 applications show now."
    )

    assert issues.doc["status"] == "CLOSED"
    assert issues.doc["resolution"]["message"] == "Fixed — your 53 applications show now."
    assert issues.doc["resolution"]["responded_by"]["name"] == "Priya"
    assert issues.doc["resolved_at"] is not None
    assert len(issues.doc["resolution_history"]) == 1


@pytest.mark.asyncio
async def test_a_second_reply_is_added_not_overwritten(issues):
    await admin_issue_service.update_admin_issue_status(str(ISSUE_ID), "CLOSED", ADMIN, response="First answer.")
    await admin_issue_service.update_admin_issue_status(str(ISSUE_ID), "IN_PROGRESS", ADMIN, response="Reopening to check.")

    assert [reply["message"] for reply in issues.doc["resolution_history"]] == ["First answer.", "Reopening to check."]
    assert issues.doc["status"] == "IN_PROGRESS"
    assert issues.doc["resolved_at"] is None


@pytest.mark.asyncio
async def test_reopening_without_a_note_writes_no_reply(issues):
    await admin_issue_service.update_admin_issue_status(str(ISSUE_ID), "IN_PROGRESS", ADMIN)

    assert "resolution" not in issues.doc
    assert "$push" not in issues.updates[0]


# ---- what the student is shown ----

def test_the_student_sees_the_answer_but_not_the_admins_email():
    shaped = _student_issue({
        "_id": ISSUE_ID,
        "status": "CLOSED",
        "resolution": {"message": "Sorted.", "responded_at": "now", "responded_by": ADMIN},
        "resolution_history": [{"message": "Sorted.", "responded_at": "now", "responded_by": ADMIN}],
        "updated_by": ADMIN,
    })

    assert shaped["resolution"] == {"message": "Sorted.", "responded_at": "now", "responded_by": "Priya"}
    assert shaped["replies"] == [shaped["resolution"]]
    assert shaped["updated_by"] == {"name": "Priya"}  # the admin's id and email are dropped
    assert "resolution_history" not in shaped
    assert "priya@example.com" not in str(shaped)


def test_a_students_own_reopen_keeps_its_record():
    """Only an admin's identity is reduced - the student's own is their own."""
    student = {"id": "stu-1", "name": "Asha", "email": "asha@example.com"}
    shaped = _student_issue({
        "_id": ISSUE_ID, "status": "IN_PROGRESS", "updated_by_type": "STUDENT", "updated_by": student,
    })

    assert shaped["updated_by"] == student


def test_an_unanswered_issue_has_no_resolution():
    shaped = _student_issue({"_id": ISSUE_ID, "status": "IN_PROGRESS"})

    assert shaped["resolution"] is None
    assert shaped["replies"] == []
