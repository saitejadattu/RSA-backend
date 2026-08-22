from datetime import datetime, timezone

import pytest

from app.services import opportunity_counter_service


class _FakeCursor:
    def __init__(self, items):
        self.items = items
        self._index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self._index]
        self._index += 1
        return item

    async def to_list(self, length=None):
        return list(self.items)


class _FakeApplicationsCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    async def aggregate(self, pipeline):
        return _FakeCursor(self.docs)

    def find(self, query, projection=None):
        return _FakeCursor(self.docs)


class _FakeOpportunitiesCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    async def find_one(self, query, projection=None):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                return dict(doc)
        return None

    async def update_one(self, query, update):
        for doc in self.docs:
            if doc.get("_id") == query.get("_id"):
                doc.update(update.get("$set", {}))
                return type("Result", (), {"matched_count": 1})()
        return type("Result", (), {"matched_count": 0})()


class _FakeDB:
    def __init__(self, apps=None, opps=None):
        self.applications = _FakeApplicationsCollection(apps or [])
        self.hiring_opportunities = _FakeOpportunitiesCollection(opps or [])

    def __getitem__(self, name):
        if name == "applications":
            return self.applications
        if name == "hiring_opportunities":
            return self.hiring_opportunities
        raise KeyError(name)


@pytest.mark.asyncio
async def test_refresh_opportunity_counts_uses_dashboard_definition(monkeypatch):
    opp_id = "opp-1"
    db = _FakeDB(
        apps=[
            {"_id": "a1", "student_id": 1, "opportunity_id": opp_id, "application_details": {"interested": True}, "current_status": "APPLIED"},
            {"_id": "a2", "student_id": 2, "opportunity_id": opp_id, "application_details": {"interested": False}, "current_status": "APPLIED"},
            {"_id": "a3", "student_id": 3, "opportunity_id": opp_id, "application_details": {"interested": True}, "current_status": "SHORTLISTED"},
            {"_id": "a4", "student_id": 4, "opportunity_id": opp_id, "application_details": {"interested": True}, "current_status": "NOT_SHORTLISTED"},
            {"_id": "a5", "student_id": 5, "opportunity_id": opp_id, "is_interested": True, "status": "shortlisted"},
            {"_id": "a6", "student_id": 6, "opportunity_id": opp_id, "is_interested": False, "status": "shortlisted"},
        ],
        opps=[{"_id": opp_id, "application_count": 0, "shortlists_count": 0}],
    )
    monkeypatch.setattr(opportunity_counter_service, "get_database", lambda: db)

    result = await opportunity_counter_service.refresh_opportunity_counts(opp_id)

    assert result == {"application_count": 4, "shortlists_count": 2}
    assert db.hiring_opportunities.docs[0]["application_count"] == 4
    assert db.hiring_opportunities.docs[0]["shortlists_count"] == 2


@pytest.mark.asyncio
async def test_refresh_opportunity_counts_counts_shortlist_evidence_once(monkeypatch):
    opp_id = "opp-2"
    db = _FakeDB(
        apps=[
            {"_id": "b1", "student_id": 1, "opportunity_id": opp_id, "application_details": {"interested": True}, "current_status": "SHORTLISTED", "shortlist": {"is_shortlisted": True}, "screening": {"decision": "shortlisted"}},
            {"_id": "b2", "student_id": 2, "opportunity_id": opp_id, "application_details": {"interested": True}, "current_status": "NOT_SHORTLISTED", "screening": {"decision": "not_shortlisted"}},
        ],
        opps=[{"_id": opp_id, "application_count": 0, "shortlists_count": 0}],
    )
    monkeypatch.setattr(opportunity_counter_service, "get_database", lambda: db)

    result = await opportunity_counter_service.refresh_opportunity_counts(opp_id)

    assert result["application_count"] == 2
    assert result["shortlists_count"] == 1


@pytest.mark.asyncio
async def test_refresh_opportunity_counts_missing_opportunity_is_safe(monkeypatch):
    db = _FakeDB(apps=[], opps=[])
    monkeypatch.setattr(opportunity_counter_service, "get_database", lambda: db)

    result = await opportunity_counter_service.refresh_opportunity_counts("missing")

    assert result == {"application_count": 0, "shortlists_count": 0}


@pytest.mark.asyncio
async def test_master_import_does_not_write_shortlists_count():
    from scripts.import_company_master import opportunity_fields

    fields = opportunity_fields({"# shortlists": "12"})

    assert "shortlists_count" not in fields
    assert fields["date_of_sharing_profiles"] is None


@pytest.mark.asyncio
async def test_update_application_status_triggers_counter_refresh(monkeypatch):
    calls = []

    async def fake_refresh(opportunity_id):
        calls.append(opportunity_id)
        return {"application_count": 1, "shortlists_count": 0}

    monkeypatch.setattr(opportunity_counter_service, "refresh_opportunity_counts", fake_refresh)

    from app.services import status_history_service

    class _FakeAppCollection:
        async def find_one(self, query, projection=None):
            return {"_id": "507f1f77bcf86cd799439011", "opportunity_id": "opp-9", "application_details": {"interested": True}, "current_status": "APPLIED"}

        async def update_one(self, query, update):
            return None

    class _FakeDB:
        def __getitem__(self, name):
            if name == "applications":
                return _FakeAppCollection()
            if name == "status_history":
                return type("History", (), {"insert_one": lambda self, doc: None})()
            raise KeyError(name)

    class _FakeHistoryCollection:
        async def insert_one(self, doc):
            return type("Result", (), {"inserted_id": "hist-1"})()

    class _FakeDB:
        def __getitem__(self, name):
            if name == "applications":
                return _FakeAppCollection()
            if name == "status_history":
                return _FakeHistoryCollection()
            raise KeyError(name)

    monkeypatch.setattr(status_history_service, "get_database", lambda: _FakeDB())

    await status_history_service.update_application_status(
        "507f1f77bcf86cd799439011",
        type("Payload", (), {"new_status": "SHORTLISTED", "reason": None, "notes": None, "changed_by": None, "changed_by_role": "admin", "source": "manual"})(),
    )

    assert calls == ["opp-9"]
