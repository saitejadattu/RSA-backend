from datetime import datetime, timezone

import pytest
from bson import ObjectId

from app.services.sheet_import_service import find_existing_master_opportunity


class FakeOpportunityCollection:
    def __init__(self, documents):
        self.documents = documents

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if query.get("_id") is not None and document.get("_id") != query["_id"]:
                continue
            if query.get("company_id") is not None and document.get("company_id") != query["company_id"]:
                continue
            if query.get("role_key") is not None:
                role_query = query["role_key"]
                if isinstance(role_query, dict):
                    if document.get("role_key") not in role_query.get("$in", []):
                        continue
                elif document.get("role_key") != role_query:
                    continue
            if query.get("opportunity_key") is not None and document.get("opportunity_key") != query["opportunity_key"]:
                continue
            date_query = query.get("opportunity_received_at")
            if date_query:
                value = document.get("opportunity_received_at")
                if not value or value < date_query["$gte"] or value >= date_query["$lt"]:
                    continue
            return {key: document.get(key) for key in (projection or document) if key in document}
        return None


class FakeDatabase:
    def __init__(self, documents):
        self.opportunities = FakeOpportunityCollection(documents)

    def __getitem__(self, name):
        return self.opportunities


@pytest.mark.asyncio
async def test_same_company_same_date_unknown_role_is_upgraded():
    company_id = ObjectId()
    existing = {"_id": ObjectId(), "company_id": company_id, "role": "unknown", "role_key": "unknown", "opportunity_key": "20-aug-2026-00-00" , "opportunity_received_at": datetime(2026, 8, 20, 9, tzinfo=timezone.utc)}
    result = await find_existing_master_opportunity(
        FakeDatabase([existing]),
        company_id=company_id,
        role_key="flutter-intern",
        opportunity_key="20-aug-2026-12-00",
        opportunity_received_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    assert result["_id"] == existing["_id"]


@pytest.mark.asyncio
async def test_same_company_different_date_creates_new_opportunity():
    company_id = ObjectId()
    existing = {"_id": ObjectId(), "company_id": company_id, "role_key": "unknown", "opportunity_received_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}
    result = await find_existing_master_opportunity(
        FakeDatabase([existing]),
        company_id=company_id,
        role_key="flutter-intern",
        opportunity_key="21-aug-2026-00-00",
        opportunity_received_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )
    assert result is None


@pytest.mark.asyncio
async def test_same_company_same_date_real_role_is_not_overwritten():
    company_id = ObjectId()
    existing = {"_id": ObjectId(), "company_id": company_id, "role_key": "react-intern", "opportunity_key": "20-aug-2026-00-00", "opportunity_received_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}
    result = await find_existing_master_opportunity(
        FakeDatabase([existing]),
        company_id=company_id,
        role_key="flutter-intern",
        opportunity_key="20-aug-2026-12-00",
        opportunity_received_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    assert result is None


@pytest.mark.asyncio
async def test_reimporting_same_real_role_uses_exact_match():
    company_id = ObjectId()
    existing = {"_id": ObjectId(), "company_id": company_id, "role_key": "flutter-intern", "opportunity_key": "20-aug-2026-12-00", "opportunity_received_at": datetime(2026, 8, 20, 12, tzinfo=timezone.utc)}
    result = await find_existing_master_opportunity(
        FakeDatabase([existing]),
        company_id=company_id,
        role_key="flutter-intern",
        opportunity_key="20-aug-2026-12-00",
        opportunity_received_at=datetime(2026, 8, 20, 12, tzinfo=timezone.utc),
    )
    assert result["_id"] == existing["_id"]
