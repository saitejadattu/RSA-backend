"""Applied / Shortlisted for a list of openings, counted from the applications.

The dashboard used to read the counter stored on each opening, which is blank on
everything imported before counters existed - so an opening with a real
shortlist showed "0" and no percentage.
"""
import pytest

from app.services import opportunity_counter_service
from app.services.opportunity_counter_service import (
    APPLICATION_COUNT_FILTER,
    SHORTLIST_COUNT_FILTER,
    counts_by_opportunity,
)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class Applications:
    """Returns a canned group result per call and remembers the pipelines."""

    def __init__(self, results):
        self.results = list(results)
        self.pipelines = []

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return Cursor(self.results.pop(0))


class DB:
    def __init__(self, applications):
        self.applications = applications

    def __getitem__(self, name):
        assert name == "applications"
        return self.applications


@pytest.fixture
def db(monkeypatch):
    applications = Applications([
        [{"_id": "opp-1", "n": 43}, {"_id": "opp-2", "n": 52}],   # applied
        [{"_id": "opp-1", "n": 3}],                                # shortlisted
    ])
    monkeypatch.setattr(opportunity_counter_service, "get_database", lambda: DB(applications))
    return applications


@pytest.mark.asyncio
async def test_counts_come_from_the_applications(db):
    counts = await counts_by_opportunity(["opp-1", "opp-2"])

    assert counts["opp-1"] == {"application_count": 43, "shortlists_count": 3}
    # An opening nobody shortlisted reports 0 - not a missing key the caller
    # would render as "no data".
    assert counts["opp-2"] == {"application_count": 52, "shortlists_count": 0}


@pytest.mark.asyncio
async def test_shortlist_count_reuses_the_stored_counter_filters(db):
    """Same filters refresh_opportunity_counts uses, so the live numbers and the
    stored ones can never disagree."""
    await counts_by_opportunity(["opp-1", "opp-2"])

    applied_match, shortlisted_match = (pipeline[0]["$match"] for pipeline in db.pipelines)
    assert applied_match == {"opportunity_id": {"$in": ["opp-1", "opp-2"]}, **APPLICATION_COUNT_FILTER}
    assert shortlisted_match["$and"][1:] == [APPLICATION_COUNT_FILTER, SHORTLIST_COUNT_FILTER]
    for pipeline in db.pipelines:
        assert pipeline[1]["$group"]["_id"] == "$opportunity_id"


@pytest.mark.asyncio
async def test_no_openings_means_no_queries(monkeypatch):
    applications = Applications([])
    monkeypatch.setattr(opportunity_counter_service, "get_database", lambda: DB(applications))

    assert await counts_by_opportunity([]) == {}
    assert applications.pipelines == []
