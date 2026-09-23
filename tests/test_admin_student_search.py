"""The admin student search runs in the database, not over the sent page.

The list is capped (500 by default). Filtering only what was sent meant every
student past the cap was unfindable - with 882 students, nobody after "P".
"""
from app.services.admin_dashboard_service import student_search_filter


def test_no_search_matches_every_student():
    assert student_search_filter(None) == {}
    assert student_search_filter("   ") == {}


def test_name_email_and_phone_are_all_searched():
    fields = [list(clause)[0] for clause in student_search_filter("tanisha")["$or"]]

    assert fields == ["name", "email", "phone"]


def test_name_and_email_ignore_case_but_phone_does_not():
    clauses = {list(clause)[0]: list(clause.values())[0] for clause in student_search_filter("Asha")["$or"]}

    assert clauses["name"]["$options"] == "i"
    assert clauses["email"]["$options"] == "i"
    assert "$options" not in clauses["phone"]  # digits have no case


def test_a_typed_dot_is_a_dot_not_a_wildcard():
    """Otherwise "a.b" would match "arb", and a "+" would be a syntax error."""
    pattern = student_search_filter("a.b+c")["$or"][0]["name"]["$regex"]

    assert pattern == r"a\.b\+c"


def test_surrounding_space_is_ignored():
    assert student_search_filter("  Tanisha  ")["$or"][0]["name"]["$regex"] == "Tanisha"
