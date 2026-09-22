"""The admin 'view as student' routes serve the student's own payloads.

They exist so an admin looking at a student sees the student's screen, not a
second rendering of the same data - so each one must call the very service the
/students/me route calls, and must never expose more than the student sees.
"""
import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.routes import admin
from app.services import student_dashboard_service

STUDENT_ID = ObjectId()


class Students:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query, projection=None):
        return next((doc for doc in self.docs if doc["_id"] == query.get("_id")), None)


class DB:
    def __init__(self, docs):
        self.students = Students(docs)

    def __getitem__(self, name):
        assert name == "students"
        return self.students


@pytest.fixture
def student(monkeypatch):
    doc = {
        "_id": STUDENT_ID, "name": "Asha", "email": "asha@example.com", "phone": "999",
        "password_hash": "$2b$12$secret", "force_password_reset": True,
    }
    monkeypatch.setattr(student_dashboard_service, "get_database", lambda: DB([doc]))
    return doc


@pytest.mark.asyncio
async def test_dashboard_is_the_students_own_payload(student, monkeypatch):
    seen = {}

    async def fake_dashboard(loaded):
        seen["student"] = loaded
        return {"summary": {"total_applications": 3}}

    monkeypatch.setattr(admin, "get_student_dashboard", fake_dashboard)

    result = await admin.student_view_dashboard(str(STUDENT_ID))

    assert seen["student"]["name"] == "Asha"
    assert result == {"summary": {"total_applications": 3}}


@pytest.mark.asyncio
async def test_reports_come_from_the_student_facing_service(student, monkeypatch):
    """Which only ever returns reports an admin has published to the student."""
    seen = {}

    async def fake_reports(student_id):
        seen["id"] = student_id
        return [{"id": "r1"}]

    monkeypatch.setattr(admin, "list_student_reports", fake_reports)

    assert await admin.student_view_reports(str(STUDENT_ID)) == [{"id": "r1"}]
    assert seen["id"] == STUDENT_ID


@pytest.mark.asyncio
async def test_issues_are_that_students_issues(student, monkeypatch):
    async def fake_issues(loaded):
        return [{"id": "i1", "student": loaded["name"]}]

    monkeypatch.setattr(admin, "list_student_issues", fake_issues)

    assert await admin.student_view_issues(str(STUDENT_ID)) == [{"id": "i1", "student": "Asha"}]


@pytest.mark.asyncio
async def test_profile_carries_no_credentials(student):
    result = await admin.student_view_profile(str(STUDENT_ID))

    assert result["name"] == "Asha"
    assert result["id"] == str(STUDENT_ID)  # an ObjectId would not survive the response
    assert "password_hash" not in result
    assert "force_password_reset" not in result


@pytest.mark.asyncio
async def test_unknown_student_is_404_not_an_empty_dashboard(student):
    with pytest.raises(HTTPException) as error:
        await admin.student_view_dashboard(str(ObjectId()))

    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_a_malformed_id_is_rejected(student):
    with pytest.raises(HTTPException) as error:
        await admin.student_view_dashboard("not-an-id")

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_practice_questions_still_check_the_student_exists(student, monkeypatch):
    """They carry no personal data, but a bad id should still 404 rather than
    quietly serving the shared bank."""
    async def fake_questions(**kwargs):
        return {"questions": [], "category": kwargs["category"]}

    monkeypatch.setattr(admin, "student_practice_questions", fake_questions)

    served = await admin.student_view_practice_questions(str(STUDENT_ID), category="dsa")
    assert served == {"questions": [], "category": "dsa"}
    with pytest.raises(HTTPException) as error:
        await admin.student_view_practice_questions(str(ObjectId()))
    assert error.value.status_code == 404
