from datetime import datetime, timezone

from bson import ObjectId

from app.services.sheet_import_service import MasterIndex


def index_with(*documents):
    return MasterIndex({document["company_id"]: "acme" for document in documents}, list(documents))


def test_same_company_same_date_unknown_role_is_upgraded():
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role": "unknown", "role_key": "unknown", "opportunity_key": "20-aug-2026-00-00", "opportunity_received_at": datetime(2026, 8, 20, 9, tzinfo=timezone.utc)}

    result = index_with(existing).find("acme", "flutter-intern", "20-aug-2026-12-00", datetime(2026, 8, 20, 12, tzinfo=timezone.utc))

    assert result is existing


def test_naive_stored_date_is_treated_as_utc():
    # Mongo returns dates without tzinfo; comparing them must not raise or miss.
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role_key": "unknown", "opportunity_key": "x", "opportunity_received_at": datetime(2026, 8, 20, 9)}

    result = index_with(existing).find("acme", "flutter-intern", "y", datetime(2026, 8, 20, 12, tzinfo=timezone.utc))

    assert result is existing


def test_same_company_different_date_creates_new_opportunity():
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role_key": "unknown", "opportunity_received_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}

    result = index_with(existing).find("acme", "flutter-intern", "21-aug-2026-00-00", datetime(2026, 8, 21, tzinfo=timezone.utc))

    assert result is None


def test_same_company_same_date_real_role_is_not_overwritten():
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role_key": "react-intern", "opportunity_key": "20-aug-2026-00-00", "opportunity_received_at": datetime(2026, 8, 20, tzinfo=timezone.utc)}

    result = index_with(existing).find("acme", "flutter-intern", "20-aug-2026-12-00", datetime(2026, 8, 20, 12, tzinfo=timezone.utc))

    assert result is None


def test_reimporting_same_real_role_uses_exact_match():
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role_key": "flutter-intern", "opportunity_key": "20-aug-2026-12-00", "opportunity_received_at": datetime(2026, 8, 20, 12, tzinfo=timezone.utc)}

    result = index_with(existing).find("acme", "flutter-intern", "20-aug-2026-12-00", datetime(2026, 8, 20, 12, tzinfo=timezone.utc))

    assert result is existing


def test_an_upgraded_opening_is_not_upgraded_again_by_a_second_role():
    existing = {"_id": ObjectId(), "company_id": ObjectId(), "role_key": "unknown", "opportunity_key": "a", "opportunity_received_at": datetime(2026, 8, 20, 9, tzinfo=timezone.utc)}
    index = index_with(existing)
    day = datetime(2026, 8, 20, 12, tzinfo=timezone.utc)

    index.record("acme", index.find("acme", "flutter-intern", "b", day), {"role_key": "flutter-intern", "opportunity_key": "b", "opportunity_received_at": day})

    assert index.find("acme", "react-intern", "c", day) is None
    assert index.find("acme", "flutter-intern", "b", day) is existing


def test_recorded_new_company_and_opening_are_visible_to_later_rows():
    index = MasterIndex({}, [])
    day = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)

    index.record("globex", None, {"role_key": "sde", "opportunity_key": "k", "opportunity_received_at": day})

    assert index.has_company("globex")
    assert index.find("globex", "sde", "k", day) is not None
