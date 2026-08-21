from datetime import datetime

import pytest
from bson import ObjectId
from pydantic import ValidationError

from app.schemas.student import StudentIssueCreate
from app.services import student_issue_service
from app.utils.dependencies import get_current_student


class FakeInsertResult:
    def __init__(self, inserted_id):
        self.inserted_id = inserted_id


class FakeIssuesCollection:
    def __init__(self):
        self.document = None

    async def insert_one(self, document):
        self.document = dict(document)
        inserted_id = ObjectId()
        return FakeInsertResult(inserted_id)


class FakeDatabase:
    def __init__(self):
        self.issues = FakeIssuesCollection()

    def __getitem__(self, name):
        assert name == "student_issues"
        return self.issues


def valid_payload(**overrides):
    values = {
        "title": "Interview status is not updated",
        "description": "I completed my interview but the dashboard still shows pending.",
        "category": "INTERVIEW",
    }
    values.update(overrides)
    return StudentIssueCreate(**values)


@pytest.mark.asyncio
async def test_authenticated_student_creates_owned_open_issue(monkeypatch):
    database = FakeDatabase()
    monkeypatch.setattr(student_issue_service, "get_database", lambda: database)
    student_id = ObjectId()
    student = {"_id": student_id, "name": "Student One"}

    result = await student_issue_service.create_student_issue(student, valid_payload())

    assert result["student_id"] == str(student_id)
    assert result["status"] == "OPEN"
    assert result["category"] == "INTERVIEW"
    assert isinstance(database.issues.document["created_at"], datetime)
    assert isinstance(database.issues.document["updated_at"], datetime)
    assert database.issues.document["student_id"] == student_id


@pytest.mark.parametrize("field", ["title", "description"])
def test_issue_text_is_required_and_not_blank(field):
    with pytest.raises(ValidationError):
        valid_payload(**{field: "   "})


def test_issue_category_is_validated():
    with pytest.raises(ValidationError):
        valid_payload(category="INVALID")


@pytest.mark.asyncio
async def test_unauthenticated_student_request_is_rejected():
    with pytest.raises(Exception) as error:
        await get_current_student(None)
    assert getattr(error.value, "status_code", None) == 401
