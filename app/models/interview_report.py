import re
from datetime import datetime, timezone
from typing import Any


TRANSCRIPT_SOURCES = {"paste", "upload", "google_doc"}

QUESTION_CATEGORIES = {
    # core engineering
    "dsa",
    "python",
    "javascript",
    "react",
    "nodejs",
    "sql",
    "mongodb",
    "system_design",
    # devops / cloud - real interviews lean heavily here
    "devops",
    "docker",
    "kubernetes",
    "aws",
    "cloud",
    "cicd",
    "terraform",
    "linux",
    "networking",
    "security",
    "monitoring",
    # ai
    "genai",
    "computer_vision",
    # non-technical
    "project",
    "behavioral",
    "hr",
    "other",
}

DIFFICULTIES = {"easy", "medium", "hard"}

# What KIND of question it is, independent of its topic.
#   conceptual  - "What is a Kubernetes pod?"
#   scenario    - "Your ALB returns 503. What could be the reason?"
#   coding      - write/trace code
#   project     - about the candidate's own project
#   followup    - "Which one?", "That's on your localhost?" - only meaningful
#                 in the moment, never reusable
#   behavioral  - "Tell me about a failure"
QUESTION_TYPES = {"conceptual", "scenario", "coding", "project", "followup", "behavioral", "other"}

QUESTION_TYPE_ALIASES = {
    "concept": "conceptual",
    "theory": "conceptual",
    "theoretical": "conceptual",
    "definition": "conceptual",
    "situational": "scenario",
    "situation": "scenario",
    "case": "scenario",
    "case_study": "scenario",
    "troubleshooting": "scenario",
    "debugging": "scenario",
    "code": "coding",
    "programming": "coding",
    "dsa": "coding",
    "resume": "project",
    "experience": "project",
    "follow_up": "followup",
    "clarification": "followup",
    "hr": "behavioral",
}


def normalize_question_type(value: str | None) -> str:
    text = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if text in QUESTION_TYPES:
        return text
    return QUESTION_TYPE_ALIASES.get(text, "other")
CORRECTNESS = {"correct", "partial", "incorrect", "not_answered"}
VERDICTS = {"strong", "average", "weak"}

AI_STATUSES = {"not_started", "in_progress", "completed", "failed"}

SPEAKER_ROLES = {"student", "interviewer", "unknown"}

# Category aliases the model may return; map onto the canonical set above.
CATEGORY_ALIASES = {
    "data structures": "dsa",
    "data structures and algorithms": "dsa",
    "algorithms": "dsa",
    "ds": "dsa",
    "js": "javascript",
    "node": "nodejs",
    "node.js": "nodejs",
    "reactjs": "react",
    "react.js": "react",
    "mongo": "mongodb",
    "database": "sql",
    "databases": "sql",
    "design": "system_design",
    "systemdesign": "system_design",
    "gen ai": "genai",
    "ai": "genai",
    "ml": "genai",
    "llm": "genai",
    "rag": "genai",
    "nlp": "genai",
    "projects": "project",
    "resume": "project",
    "behaviour": "behavioral",
    "behaviour_questions": "behavioral",
    "communication": "behavioral",
    "introduction": "hr",
    # devops / cloud
    "dev ops": "devops",
    "ci/cd": "cicd",
    "ci_cd": "cicd",
    "ci cd": "cicd",
    "jenkins": "cicd",
    "github actions": "cicd",
    "gitops": "cicd",
    "k8s": "kubernetes",
    "kubernates": "kubernetes",
    "containers": "docker",
    "containerization": "docker",
    "amazon web services": "aws",
    "ec2": "aws",
    "s3": "aws",
    "eks": "aws",
    "lambda": "aws",
    "vpc": "networking",
    "infrastructure": "terraform",
    "iac": "terraform",
    "observability": "monitoring",
    "prometheus": "monitoring",
    "grafana": "monitoring",
    "opencv": "computer_vision",
    "cv": "computer_vision",
}

# Google Meet's speech-to-text mangles technical terms. Left unfixed these
# corrupt question_key, so "what is genkins" and "what is jenkins" would never
# dedupe into one bank entry. The model is told to normalize too; this is the
# deterministic backstop for the terms we have actually seen.
ASR_TERM_FIXES = {
    r"\bgenkins\b": "Jenkins",
    r"\bjenkin\b": "Jenkins",
    r"\bcubernetes\b": "Kubernetes",
    r"\bkubernates\b": "Kubernetes",
    r"\bkubernetis\b": "Kubernetes",
    r"\bterapform\b": "Terraform",
    r"\bterrafrom\b": "Terraform",
    r"\bpromatis\b": "Prometheus",
    r"\bprometheous\b": "Prometheus",
    r"\bgraphana\b": "Grafana",
    r"\bdocker compost\b": "Docker Compose",
    r"\bdocker compos\b": "Docker Compose",
    r"\bvtc\b": "VPC",
    r"\brad based\b": "RAG based",
    r"\brack\b": "RAG",
    r"\bdrag based\b": "RAG based",
    r"\bestim\b": "S3",
    r"\bazour\b": "Azure",
    r"\bec\b(?= and ecs)": "EC2",
}


def fix_asr_terms(text: str | None) -> str:
    """Repair known speech-to-text manglings of technical terms."""
    result = text or ""
    for pattern, replacement in ASR_TERM_FIXES.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


# A question that asks the candidate to reveal something on their screen, or
# that leans on "your <thing>", cannot be practised by anyone else - no matter
# how cleanly it is phrased. The model rewrites the grammar but still lets these
# through ("Can you show me the knowledge base that your tool crawled?"), so
# this is the deterministic backstop for the practice bank.
# The signal is the VERB, not the word "your". "Is your application deployed or
# running locally?" is a perfectly good generic question, while "Show me your
# chatbot" is dead - both contain "your". Matching on "your <noun>" would throw
# away good material, which is worse than letting one borderline question
# through, since the model's own is_reusable judgement is the primary gate.
CONTEXT_BOUND_PATTERNS = (
    r"\b(?:can|could|will|would)\s+(?:you|we)\s+(?:please\s+)?(?:show|demo|open|run|share|display|walk\s+me)\b",
    r"\bshow\s+me\b",
    r"\bdemo\b",
    r"\blet'?s\s+(?:try|see|look)\b",
    r"\btry\s+(?:it|this|that|out)\b",
    r"\bopen\s+(?:that|this|the)\b",
    r"\b(?:this|that)\s+(?:screen|repo|repository|codebase|file)\b",
    r"\bwhich\s+one\b",
    r"\bcome\s+again\b",
    r"\brepeat\s+the\s+(?:question|company)\b",
    r"\brefer(?:ring)?\s+to\b",
)


def looks_context_bound(text: str | None) -> bool:
    """True when a question only makes sense inside that conversation."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return True
    return any(re.search(pattern, lowered) for pattern in CONTEXT_BOUND_PATTERNS)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


STOP_PREFIXES = re.compile(
    r"^(?:okay|ok|so|and|now|yeah|right|uh|um|next|last question|alright)\b[\s,.]*",
    re.IGNORECASE,
)


def question_key(text: str | None) -> str:
    """Normalized fingerprint so the same question asked at different companies
    collapses onto one bank entry.

    ASR fixes are applied first, otherwise "what is genkins pipeline" and
    "what is jenkins pipeline" would be two separate bank entries. Filler
    prefixes ("Okay. So ...") are stripped for the same reason.
    """
    cleaned = fix_asr_terms(text or "").strip().lower()
    previous = None
    while previous != cleaned:  # "Okay. So, why ..." needs more than one pass
        previous = cleaned
        cleaned = STOP_PREFIXES.sub("", cleaned).strip()
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_category(value: str | None) -> str:
    text = (value or "").strip().lower().replace("-", "_")
    if text in QUESTION_CATEGORIES:
        return text
    alias = CATEGORY_ALIASES.get(text) or CATEGORY_ALIASES.get(text.replace("_", " "))
    return alias if alias in QUESTION_CATEGORIES else "other"


def normalize_difficulty(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    return text if text in DIFFICULTIES else None


def normalize_correctness(value: str | None) -> str:
    text = (value or "").strip().lower().replace(" ", "_")
    if text in CORRECTNESS:
        return text
    if text in {"right", "accurate"}:
        return "correct"
    if text in {"partially_correct", "partially", "incomplete"}:
        return "partial"
    if text in {"wrong", "inaccurate"}:
        return "incorrect"
    if text in {"skipped", "no_answer", "none"}:
        return "not_answered"
    return "partial"


def normalize_verdict(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in VERDICTS:
        return text
    if text in {"good", "excellent", "great"}:
        return "strong"
    if text in {"poor", "bad", "needs_improvement"}:
        return "weak"
    return "average"


def clamp(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def default_report_overall() -> dict[str, Any]:
    return {"score": None, "verdict": None, "summary": None}
