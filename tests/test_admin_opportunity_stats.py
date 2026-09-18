"""The admin cards count an opening's own applications.

They used to read the stored shortlists_count counter, which a master import
could overwrite with the CRM's "# shortlists" cell - so an opening with three
shortlisted candidates showed "Shortlisted 0" above a list of three.
"""
from app.services.admin_company_service import _blank_counts, _tally


def tally(*applications) -> dict:
    counts = _blank_counts()
    for application in applications:
        _tally(counts, application)
    return counts


def test_shortlisted_applications_are_counted():
    counts = tally(
        {"current_status": "SHORTLISTED"},
        {"current_status": "SHORTLISTED"},
        {"current_status": "NOT_SHORTLISTED"},
    )

    assert counts["shortlisted_count"] == 2
    assert counts["not_shortlisted_count"] == 1
    assert counts["applied_count"] == 3
    assert counts["response_count"] == 3


def test_shortlist_is_counted_the_way_the_stored_counter_counts_it():
    """Same definition as opportunity_counter_service, so a card and the stored
    counter can never disagree."""
    counts = tally(
        {"current_status": "APPLIED", "shortlist": {"is_shortlisted": True}},
        {"current_status": "APPLIED", "screening": {"decision": "shortlisted"}},
        {"current_status": "APPLIED"},
    )

    assert counts["shortlisted_count"] == 2
    assert counts["not_shortlisted_count"] == 0


def test_not_shortlisted_is_not_reported_as_rejected():
    counts = tally({"current_status": "NOT_SHORTLISTED"}, {"current_status": "REJECTED"})

    assert counts["not_shortlisted_count"] == 1
    assert counts["rejected_count"] == 1


def test_an_uninterested_response_is_not_an_application():
    counts = tally({"current_status": "APPLIED", "application_details": {"interested": False}})

    assert counts == {**_blank_counts(), "response_count": 1, "not_interested_count": 1}
