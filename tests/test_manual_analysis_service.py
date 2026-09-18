from bson import ObjectId
import pytest

from app.schemas.interview_report import ManualAnalysisRequest
from app.services import manual_analysis_service


class Cursor:
    def __init__(self, items):
        self.items = items

    async def to_list(self, length=None):
        return list(self.items)


class Collection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self.writes = []

    async def find_one(self, query, projection=None):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                return dict(document)
        return None

    def find(self, query, projection=None):
        def matches(document):
            for key, value in query.items():
                if isinstance(value, dict) and "$in" in value:
                    if document.get(key) not in value["$in"]:
                        return False
                elif document.get(key) != value:
                    return False
            return True
        return Cursor([dict(document) for document in self.documents if matches(document)])

    async def find_one_and_update(self, query, update, **kwargs):
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update.get("$set", {}))
                self.writes.append((query, update))
                return dict(document)
        document = {**query, **update.get("$set", {}), **update.get("$setOnInsert", {})}
        document["_id"] = ObjectId()
        self.documents.append(document)
        self.writes.append((query, update))
        return dict(document)

    async def update_one(self, query, update, **kwargs):
        self.writes.append((query, update))
        for document in self.documents:
            if all(document.get(key) == value for key, value in query.items()):
                document.update(update.get("$set", {}))
        return None


class FakeDB:
    def __init__(self, session, application, student, reports=None):
        self.collections = {
            "interview_sessions": Collection([session]),
            "transcripts": Collection([{"_id": ObjectId(), "session_id": session["_id"], "speaker_map": [{"student_id": student["_id"], "speaker_label": student["name"], "role": "student"}]}]),
            "applications": Collection([application]),
            "students": Collection([student]),
            "interview_reports": Collection(reports),
            "questions": Collection(),
        }

    def __getitem__(self, name):
        if name == "status_history":
            raise AssertionError("manual analysis must not write status history")
        return self.collections[name]


@pytest.fixture
def context():
    session_id = ObjectId()
    student_id = ObjectId()
    application_id = ObjectId()
    company_id = ObjectId()
    opportunity_id = ObjectId()
    session = {
        "_id": session_id,
        "company_id": company_id,
        "opportunity_id": opportunity_id,
        "students": [{"student_id": student_id, "application_id": application_id}],
    }
    application = {
        "_id": application_id,
        "student_id": student_id,
        "company_id": company_id,
        "opportunity_id": opportunity_id,
    }
    student = {"_id": student_id, "name": "Siva"}
    return session_id, student_id, session, application, student


def payload_for(student_id):
    return ManualAnalysisRequest.model_validate({
        "candidates": [{
            "candidate_name": "Siva",
            "report": {
                "overall": {"score": 8, "verdict": "strong", "summary": "Good work."},
                "answers": [{"question_text": "What is Python?", "accuracy": 80, "correctness": "correct"}],
                "skill_ratings": {"python": 4},
                "communication": {"clarity": 4, "confidence": 3},
            },
        }],
        "questions": [{
            "candidate_name": "Siva",
            "question_text": "What is Python?",
            "category": "python",
            "difficulty": "easy",
            "question_type": "conceptual",
            "is_technical": True,
            "is_reusable": True,
        }],
        "company_expectations": {"expectations": "Strong fundamentals.", "focus": ["Python"]},
    })


@pytest.mark.asyncio
async def test_preview_validates_without_writes(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)

    result = await manual_analysis_service.preview_manual_analysis(str(session_id), payload_for(student_id))

    assert result["candidates"] == 1
    assert result["questions"] == 1
    assert result["company_expectations"] is True
    assert all(not collection.writes for collection in db.collections.values())


@pytest.mark.asyncio
async def test_current_manual_json_categories_preview_and_persist_as_canonical_values(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    categories = ["Testing", "Python", "AI", "Projects", "Web Development", "Programming Languages", "Algorithms", "Programming Fundamentals"]
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Siva", "report": {}}],
        "questions": [
            {"candidate_name": "Siva", "question_text": f"Question {index}", "category": category}
            for index, category in enumerate(categories)
        ],
    })

    preview = await manual_analysis_service.preview_manual_analysis(str(session_id), payload)
    result = await manual_analysis_service.save_manual_analysis(str(session_id), payload)

    assert preview["questions"] == len(categories)
    assert result["questions_saved"] == len(categories)
    assert [question["category"] for question in db.collections["questions"].documents] == [
        "other", "python", "genai", "project", "other", "other", "dsa", "programming_fundamentals"
    ]


@pytest.mark.asyncio
async def test_current_manual_json_question_types_preview_and_persist(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    types = ["coding", "comparison", "conceptual", "project", "scenario"]
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Siva", "report": {}}],
        "questions": [
            {"candidate_name": "Siva", "question_text": f"Question {index}", "question_type": value}
            for index, value in enumerate(types)
        ],
    })

    preview = await manual_analysis_service.preview_manual_analysis(str(session_id), payload)
    result = await manual_analysis_service.save_manual_analysis(str(session_id), payload)

    assert preview["questions"] == len(types)
    assert [item["normalized"] for item in preview["question_types"]] == types
    assert result["questions_saved"] == len(types)
    assert [question["question_type"] for question in db.collections["questions"].documents] == types


def test_manual_new_category_becomes_canonical_slug():
    assert manual_analysis_service.normalize_manual_category("Programming Fundamentals") == "programming_fundamentals"
    assert manual_analysis_service.normalize_manual_category("Programming-Fundamentals") == "programming_fundamentals"
    assert manual_analysis_service.normalize_manual_category("System Design / Architecture") == "system_design_architecture"


def test_manual_question_types_use_existing_values_or_new_slugs():
    assert manual_analysis_service.normalize_manual_question_type("Conceptual") == "conceptual"
    assert manual_analysis_service.normalize_manual_question_type("Coding") == "coding"
    assert manual_analysis_service.normalize_manual_question_type("Project") == "project"
    assert manual_analysis_service.normalize_manual_question_type("Comparison") == "comparison"
    assert manual_analysis_service.normalize_manual_question_type("Scenario Based") == "scenario_based"


@pytest.mark.parametrize("value", [None, "", "   ", 42, [], {}])
def test_manual_question_type_rejects_invalid_values(value):
    with pytest.raises(Exception, match="Question type"):
        manual_analysis_service.normalize_manual_question_type(value)


@pytest.mark.asyncio
async def test_preview_rejects_unknown_student(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Unknown Candidate", "report": {}}],
        "questions": [],
    })

    result = await manual_analysis_service.preview_manual_analysis(str(session_id), payload)
    assert result["candidate_preview"][0]["report"] == "Not created"
    assert result["anonymous_questions"] == 0


@pytest.mark.asyncio
async def test_save_writes_questions_report_and_expectations_only(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)

    result = await manual_analysis_service.save_manual_analysis(str(session_id), payload_for(student_id))

    assert result["candidate_reports_saved"] == 1
    assert result["questions_saved"] == 1
    assert db.collections["questions"].documents[0]["question_text"] == "What is Python?"
    assert db.collections["interview_reports"].documents[0]["visible_to_student"] is False
    assert db.collections["interview_sessions"].writes[0][1]["$set"]["company_expectations"]["focus"] == ["Python"]


@pytest.mark.asyncio
async def test_save_preserves_all_company_focus_items(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    focus = [f"Focus {index}" for index in range(10)]
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Siva", "report": {}}],
        "company_expectations": {"expectations": "Detailed.", "focus": focus},
    })

    await manual_analysis_service.save_manual_analysis(str(session_id), payload)

    assert db.collections["interview_sessions"].writes[0][1]["$set"]["company_expectations"]["focus"] == focus


@pytest.mark.asyncio
async def test_save_preserves_existing_report_visibility(monkeypatch, context):
    session_id, student_id, session, application, student = context
    existing = {"_id": ObjectId(), "session_id": session_id, "student_id": student_id, "visible_to_student": True}
    db = FakeDB(session, application, student, reports=[existing])
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)

    await manual_analysis_service.save_manual_analysis(str(session_id), payload_for(student_id))

    assert db.collections["interview_reports"].documents[0]["visible_to_student"] is True
    assert "visible_to_student" not in db.collections["interview_reports"].writes[0][1]["$set"]


@pytest.mark.asyncio
async def test_repeated_save_uses_existing_question_and_report_records(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    payload = payload_for(student_id)

    await manual_analysis_service.save_manual_analysis(str(session_id), payload)
    await manual_analysis_service.save_manual_analysis(str(session_id), payload)

    assert len(db.collections["questions"].documents) == 1
    assert len(db.collections["interview_reports"].documents) == 1


@pytest.mark.asyncio
async def test_unknown_candidate_questions_are_saved_anonymously_without_report(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Unknown Candidate", "report": {}}],
        "questions": [{
            "candidate_name": "Unknown Candidate",
            "question_text": "What is Python?",
            "category": "python",
            "difficulty": "easy",
            "question_type": "conceptual",
            "is_technical": True,
        }],
    })

    result = await manual_analysis_service.save_manual_analysis(str(session_id), payload)

    assert result["candidate_reports_saved"] == 0
    assert result["questions_saved"] == 1
    assert result["anonymous_questions"] == 1
    assert db.collections["questions"].documents[0]["asked_to"] == "Unknown Candidate"
    assert "student_id" not in db.collections["questions"].documents[0]
    assert len(db.collections["interview_reports"].documents) == 0


@pytest.mark.asyncio
async def test_preview_reports_attributed_unknown_questions_as_anonymous(monkeypatch, context):
    session_id, student_id, session, application, student = context
    db = FakeDB(session, application, student)
    monkeypatch.setattr(manual_analysis_service, "get_database", lambda: db)
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Unknown Candidate", "report": {}}],
        "questions": [{"candidate_name": "Unknown Candidate", "question_text": "What is Python?"}],
    })

    result = await manual_analysis_service.preview_manual_analysis(str(session_id), payload)

    assert result["anonymous_questions"] == 1


def test_manual_question_accepts_string_prepare_value():
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Siva", "report": {}}],
        "questions": [{"candidate_name": "Siva", "question_text": "What is Python?", "prepare": "Python basics"}],
    })

    assert payload.questions[0].prepare == ["Python basics"]


def test_manual_company_focus_accepts_more_than_six_items():
    focus = [f"Focus {index}" for index in range(10)]
    payload = ManualAnalysisRequest.model_validate({
        "candidates": [{"candidate_name": "Siva", "report": {}}],
        "company_expectations": {"expectations": "Detailed.", "focus": focus},
    })

    assert payload.company_expectations.focus == focus


def test_manual_company_focus_rejects_non_string_items():
    with pytest.raises(ValueError):
        ManualAnalysisRequest.model_validate({
            "candidates": [{"candidate_name": "Siva", "report": {}}],
            "company_expectations": {"focus": ["Python", 3]},
        })


def test_manual_schema_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        ManualAnalysisRequest.model_validate({
            "candidates": [{"student_id": str(ObjectId()), "report": {"overall": {"score": 11}}}],
            "questions": [],
        })


def test_manual_schema_rejects_database_identifiers():
    with pytest.raises(ValueError):
        ManualAnalysisRequest.model_validate({
            "candidates": [{
                "student_id": str(ObjectId()),
                "candidate_name": "Siva",
                "report": {},
            }],
        })
