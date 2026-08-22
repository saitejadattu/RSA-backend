import copy

import pytest

from scripts import backfill_opportunity_counts as backfill_script


class FakeCursor:
    def __init__(self, documents):
        self.documents = documents

    async def to_list(self, length=None):
        return list(self.documents)


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents
        self.updates = []

    def find(self, query, projection=None):
        return FakeCursor(self.documents)

    async def update_one(self, query, update):
        self.updates.append((query, update))
        for document in self.documents:
            if document["_id"] == query["_id"]:
                document.update(update["$set"])
        return None


class FakeDatabase:
    def __init__(self, opportunities, applications):
        self.collections = {
            "hiring_opportunities": FakeCollection(opportunities),
            "applications": FakeCollection(applications),
        }

    async def list_collection_names(self):
        return list(self.collections)

    def __getitem__(self, name):
        return self.collections[name]


def test_application_count_excludes_not_interested_and_uses_legacy_fields():
    applications = [
        {"application_details": {"interested": True}, "current_status": "APPLIED"},
        {"application_details": {"interested": False}, "current_status": "APPLIED"},
        {"is_interested": True, "status": "applied"},
        {"is_interested": True, "status": "not_interested"},
        {"is_interested": False, "status": "applied"},
    ]

    assert backfill_script.calculate_counts(applications)[0] == 2


def test_shortlist_count_deduplicates_multiple_evidence():
    applications = [
        {
            "application_details": {"interested": True},
            "current_status": "SHORTLISTED",
            "shortlist": {"is_shortlisted": True},
            "screening": {"decision": "shortlisted"},
        },
        {"application_details": {"interested": True}, "screening": {"decision": "shortlisted"}},
        {"application_details": {"interested": True}, "status": "shortlisted"},
    ]

    assert backfill_script.calculate_counts(applications) == (3, 3)


def test_missing_urls_do_not_create_counts():
    applications = [{"application_details": {"interested": True}, "current_status": "APPLIED"}]
    assert backfill_script.calculate_counts(applications) == (1, 0)


@pytest.mark.asyncio
async def test_dry_run_does_not_modify_mongodb(capsys):
    opportunities = [{"_id": "opp-1", "role": "Engineer", "application_count": 0, "shortlists_count": 0}]
    applications = [{"_id": "app-1", "opportunity_id": "opp-1", "application_details": {"interested": True}}]
    db = FakeDatabase(opportunities, applications)
    before = copy.deepcopy(opportunities)

    summary = await backfill_script.backfill(dry_run=True, db=db)

    assert opportunities == before
    assert db.collections["hiring_opportunities"].updates == []
    assert summary.updated == 1
    output = capsys.readouterr().out
    assert "Response sheet: NO" in output
    assert "Shortlist sheet: NO" in output


@pytest.mark.asyncio
async def test_normal_execution_updates_only_counter_fields():
    opportunities = [{
        "_id": "opp-1", "company_name": "Acme", "role": "Engineer",
        "application_count": 0, "shortlists_count": 0, "updated_at": "keep",
        "company_sheet": "https://shortlist.example/sheet",
    }]
    applications = [{
        "_id": "app-1", "opportunity_id": "opp-1",
        "application_details": {"interested": True}, "current_status": "SHORTLISTED",
    }]
    db = FakeDatabase(opportunities, applications)

    summary = await backfill_script.backfill(dry_run=False, db=db)

    assert summary.updated == 1
    assert opportunities[0]["application_count"] == 1
    assert opportunities[0]["shortlists_count"] == 1
    assert opportunities[0]["updated_at"] == "keep"
    assert db.collections["hiring_opportunities"].updates == [
        ({"_id": "opp-1"}, {"$set": {"application_count": 1, "shortlists_count": 1}})
    ]
