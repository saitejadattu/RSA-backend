"""Tests for RSA usage tracking."""

import pytest
from datetime import datetime, timezone
from bson import ObjectId
from unittest.mock import MagicMock

from app.services.rsa_usage_service import record_rsa_hit, get_admin_rsa_usage
from app.config.constants import ROLE_STUDENT, ROLE_ADMIN


class Cursor:
    def __init__(self, items):
        self.items = items

    async def to_list(self, length=None):
        return list(self.items)


class Collection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])

    async def count_documents(self, query):
        count = 0
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                count += 1
        return count

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return dict(document)
        return None

    async def insert_one(self, document):
        new_doc = dict(document)
        if "_id" not in new_doc:
            new_doc["_id"] = ObjectId()
        self.documents.append(new_doc)
        return MagicMock(inserted_id=new_doc["_id"])

    def aggregate(self, pipeline):
        result = list(self.documents)
        for stage in pipeline:
            if "$group" in stage:
                result = self._apply_group(result, stage["$group"])
            elif "$sort" in stage:
                result = self._apply_sort(result, stage["$sort"])
            elif "$lookup" in stage:
                result = self._apply_lookup(result, stage["$lookup"])
            elif "$unwind" in stage:
                result = self._apply_unwind(result, stage["$unwind"])
            elif "$project" in stage:
                result = self._apply_project(result, stage["$project"])
            elif "$count" in stage:
                result = [{"count": len(result)}]
        return Cursor(result)

    def _apply_group(self, documents, group_spec):
        groups = {}
        for doc in documents:
            group_key = doc.get(group_spec.get("_id"))
            if group_key not in groups:
                groups[group_key] = {}
            if "_id" not in groups[group_key]:
                groups[group_key]["_id"] = group_key
            for key, value in group_spec.items():
                if key != "_id":
                    if "$sum" in value:
                        groups[group_key][key] = groups[group_key].get(key, 0) + value["$sum"]
                    elif "$max" in value:
                        field = value["$max"]
                        doc_val = doc.get(field)
                        groups[group_key][key] = max(groups[group_key].get(key), doc_val) if groups[group_key].get(key) is not None else doc_val
        return list(groups.values())

    def _apply_sort(self, documents, sort_spec):
        for field, direction in reversed(list(sort_spec.items())):
            documents.sort(key=lambda x: x.get(field) or "", reverse=(direction == -1))
        return documents

    def _apply_lookup(self, documents, lookup_spec):
        for doc in documents:
            doc[lookup_spec["as"]] = []
        return documents

    def _apply_unwind(self, documents, unwind_spec):
        result = []
        field_name = unwind_spec.replace("$", "")
        for doc in documents:
            if field_name in doc and isinstance(doc[field_name], list):
                for item in doc[field_name]:
                    new_doc = dict(doc)
                    new_doc[field_name] = item
                    result.append(new_doc)
            else:
                result.append(doc)
        return result

    def _apply_project(self, documents, project_spec):
        result = []
        for doc in documents:
            projected = {}
            for field, include in project_spec.items():
                if include == 1 and field != "_id":
                    if field in doc:
                        projected[field] = doc[field]
                elif field == "_id" and include == 0:
                    continue
            result.append(projected)
        return result


class FakeDB:
    def __init__(self):
        self.collections = {
            "rsa_usage": Collection(),
            "students": Collection(),
        }

    def __getitem__(self, name):
        return self.collections[name]
    
        def get_all_collections(self):
            return self.collections


@pytest.fixture
def fake_db():
    return FakeDB()


@pytest.mark.asyncio
async def test_student_can_record_rsa_hit(fake_db, monkeypatch):
    """Test that a student can record an RSA hit."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    result = await record_rsa_hit(student_id)
    
    assert result["success"] is True
    assert await fake_db["rsa_usage"].count_documents({"student_id": student_id}) == 1


@pytest.mark.asyncio
async def test_one_rsa_usage_document_inserted_per_call(fake_db, monkeypatch):
    """Test that each call creates exactly one rsa_usage document."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await record_rsa_hit(student_id)
    
    assert await fake_db["rsa_usage"].count_documents({"student_id": student_id}) == 1


@pytest.mark.asyncio
async def test_student_id_from_record_rsa_hit(fake_db, monkeypatch):
    """Test that student_id is properly recorded."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await record_rsa_hit(student_id)
    
    hit = await fake_db["rsa_usage"].find_one({"student_id": student_id})
    assert hit is not None
    assert hit["student_id"] == student_id


@pytest.mark.asyncio
async def test_opened_at_is_stored(fake_db, monkeypatch):
    """Test that opened_at timestamp is stored."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await record_rsa_hit(student_id)
    
    hit = await fake_db["rsa_usage"].find_one({"student_id": student_id})
    assert hit is not None
    assert "opened_at" in hit
    assert isinstance(hit["opened_at"], datetime)
    assert hit["opened_at"].tzinfo is not None


@pytest.mark.asyncio
async def test_calling_twice_creates_two_hits(fake_db, monkeypatch):
    """Test that calling twice creates two separate hits."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await record_rsa_hit(student_id)
    await record_rsa_hit(student_id)
    
    hit_count = await fake_db["rsa_usage"].count_documents({"student_id": student_id})
    assert hit_count == 2


@pytest.mark.asyncio
async def test_admin_endpoint_returns_structure(fake_db, monkeypatch):
    """Test that admin endpoint returns correct structure."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    result = await get_admin_rsa_usage()
    
    assert "total_students" in result
    assert "unique_students" in result
    assert "total_hits" in result
    assert "not_opened" in result
    assert "students" in result
    assert isinstance(result["students"], list)


@pytest.mark.asyncio
async def test_total_students_correct(fake_db, monkeypatch):
    """Test total_students count is correct."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    for i in range(5):
        await fake_db["students"].insert_one({
            "_id": ObjectId(),
            "email": f"student{i}@example.com",
            "name": f"Student {i}",
            "role": ROLE_STUDENT,
        })
    
    result = await get_admin_rsa_usage()
    assert result["total_students"] == 5


@pytest.mark.asyncio
async def test_unique_students_correct(fake_db, monkeypatch):
    """Test unique_students count is correct."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_ids = []
    for i in range(5):
        sid = ObjectId()
        student_ids.append(sid)
        await fake_db["students"].insert_one({
            "_id": sid,
            "email": f"student{i}@example.com",
            "name": f"Student {i}",
            "role": ROLE_STUDENT,
        })
    
    # Record hits for first 3 students
    for sid in student_ids[:3]:
        await record_rsa_hit(sid)
    
    result = await get_admin_rsa_usage()
    assert result["unique_students"] == 3


@pytest.mark.asyncio
async def test_total_hits_correct(fake_db, monkeypatch):
    """Test total_hits count is correct."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await fake_db["students"].insert_one({
        "_id": student_id,
        "email": "student@example.com",
        "name": "Student",
        "role": ROLE_STUDENT,
    })
    
    for _ in range(5):
        await record_rsa_hit(student_id)
    
    result = await get_admin_rsa_usage()
    assert result["total_hits"] == 5


@pytest.mark.asyncio
async def test_not_opened_correct(fake_db, monkeypatch):
    """Test not_opened count is correct."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_ids = []
    for i in range(5):
        sid = ObjectId()
        student_ids.append(sid)
        await fake_db["students"].insert_one({
            "_id": sid,
            "email": f"student{i}@example.com",
            "name": f"Student {i}",
            "role": ROLE_STUDENT,
        })
    
    for sid in student_ids[:2]:
        await record_rsa_hit(sid)
    
    result = await get_admin_rsa_usage()
    assert result["not_opened"] == 3


@pytest.mark.asyncio
async def test_student_rows_have_fields(fake_db, monkeypatch):
    """Test student rows have all required fields."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await fake_db["students"].insert_one({
        "_id": student_id,
        "email": "student@example.com",
        "name": "Test Student",
        "role": ROLE_STUDENT,
    })
    await record_rsa_hit(student_id)
    
    result = await get_admin_rsa_usage()
    assert len(result["students"]) > 0
    
    row = result["students"][0]
    assert "student_id" in row
    assert "name" in row
    assert "hit_count" in row
    assert "last_opened_at" in row


@pytest.mark.asyncio
async def test_object_ids_serialized_strings(fake_db, monkeypatch):
    """Test ObjectIds are serialized as strings."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    student_id = ObjectId()
    await fake_db["students"].insert_one({
        "_id": student_id,
        "email": "student@example.com",
        "name": "Test Student",
        "role": ROLE_STUDENT,
    })
    await record_rsa_hit(student_id)
    
    result = await get_admin_rsa_usage()
    row = result["students"][0]
    
    assert isinstance(row["student_id"], str)
    assert row["student_id"] == str(student_id)


@pytest.mark.asyncio
async def test_zero_hits_not_in_array(fake_db, monkeypatch):
    """Test students with zero hits are not in the array."""
    monkeypatch.setattr("app.services.rsa_usage_service.get_database", lambda: fake_db)
    
    for i in range(3):
        await fake_db["students"].insert_one({
            "_id": ObjectId(),
            "email": f"student{i}@example.com",
            "name": f"Student {i}",
            "role": ROLE_STUDENT,
        })
    
    student_ids = [s["_id"] for s in fake_db["students"].documents]
    await record_rsa_hit(student_ids[0])
    
    result = await get_admin_rsa_usage()
    assert len(result["students"]) == 1
