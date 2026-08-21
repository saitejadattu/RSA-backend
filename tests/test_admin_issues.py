from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app.services import admin_issue_service


class Cursor:
    def __init__(self, items):
        self.items = items

    def sort(self, *args):
        return self

    def skip(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self, length=None):
        return list(self.items)


class Collection:
    def __init__(self, documents):
        self.documents = documents

    def find(self, query, projection=None):
        items = self.documents
        if "_id" in query and "$in" in query["_id"]:
            items = [doc for doc in items if doc.get("_id") in query["_id"]["$in"]]
        return Cursor(items)

    async def count_documents(self, query):
        if query == {}:
            return len(self.documents)
        return sum(1 for doc in self.documents if all(doc.get(key) == value for key, value in query.items()))

    async def find_one(self, query, projection=None):
        return next((doc for doc in self.documents if doc.get("_id") == query.get("_id")), None)


class Database:
    def __init__(self, issues, students):
        self.collections = {
            "student_issues": Collection(issues),
            "students": Collection(students),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_admin_issue_list_returns_summary_and_student(monkeypatch):
    student_id = ObjectId()
    now = datetime.now(timezone.utc)
    issues = [
        {"_id": ObjectId(), "student_id": student_id, "title": "Broken status", "category": "BUG", "status": "OPEN", "created_at": now},
        {"_id": ObjectId(), "student_id": student_id, "title": "Thanks", "category": "FEEDBACK", "status": "RESOLVED", "created_at": now},
    ]
    monkeypatch.setattr(admin_issue_service, "get_database", lambda: Database(issues, [{"_id": student_id, "name": "A Student", "email": "a@example.com"}]))

    result = await admin_issue_service.list_admin_issues()

    assert result["summary"] == {"total": 2, "open": 1, "resolved": 1}
    assert result["items"][0]["student"]["name"] == "A Student"


@pytest.mark.asyncio
async def test_admin_issue_detail_returns_description_and_student(monkeypatch):
    issue_id = ObjectId()
    student_id = ObjectId()
    issue = {"_id": issue_id, "student_id": student_id, "title": "Interview", "description": "Pending", "category": "INTERVIEW", "status": "OPEN"}
    monkeypatch.setattr(admin_issue_service, "get_database", lambda: Database([issue], [{"_id": student_id, "name": "A Student", "phone": "123456"}]))

    result = await admin_issue_service.get_admin_issue(str(issue_id))

    assert result["description"] == "Pending"
    assert result["student"]["phone"] == "123456"
