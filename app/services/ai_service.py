import copy
import json
from typing import Any

from fastapi import HTTPException, status

from app.config.settings import get_settings

SYSTEM_PROMPT = """You analyse ONE candidate's technical interview for a student placement platform.

INPUT FORMAT
Each line is:  [segment_order] Speaker Name: what they said
This is a raw Google Meet machine transcript, so expect:
  * INTERLEAVED, FRAGMENTED speech. A single question or answer is often split
    across several lines with the other person's short interjections between.
    Example: "What is Docker" ... answer ... "Compose?" is ONE question,
    "What is Docker Compose?". Reassemble fragments before judging them.
  * Filler and backchannel ("Okay.", "Uh", "do", "Why") that carry no meaning.
  * Speech-to-text errors on technical terms. Normalise them in your output:
    genkins->Jenkins, cubernetes/kubernates->Kubernetes, terapform->Terraform,
    promatis->Prometheus, graphana->Grafana, VTC->VPC, rad/rack->RAG,
    docker compost->Docker Compose, estim->S3, azour->Azure, 53->503.
    Write questions using the CORRECT technical term, not the garbled one.

DO TASK 1 FIRST AND COMPLETELY. Extract EVERY question the interviewer asked
before you evaluate the candidate at all. Missing a question is the worst
failure here - work through the transcript in order and capture each one.

TASK 1 - QUESTIONS: every question the interviewer asked this candidate.
  * question_text: REWRITE it as a clean, standalone interview question - do
    NOT copy the transcript wording. It must read like something an
    interviewer would naturally ask any candidate, in ONE sentence.
      "Building a crawler is easy but hosting 1500 websites..."
        -> "Do you have experience building scalable web crawlers?"
      "That's working on your local host?"
        -> "Is your application deployed or running locally?"
    Keep the meaning; drop the names, the demo and the small talk.
  * raw_question_text: what was actually said, reassembled but not rewritten.
  * is_technical: true for technical/coding/design/project probing; false for
    HR/behavioural/logistics ("introduce yourself", "any questions for me").
  * category: one of dsa, python, javascript, react, nodejs, sql, mongodb,
    system_design, devops, docker, kubernetes, aws, cloud, cicd, terraform,
    linux, networking, security, monitoring, genai, computer_vision, project,
    behavioral, hr, other.
  * question_type:
      conceptual - asks what something is / how it works
                   ("What is a Kubernetes pod?")
      scenario   - gives a situation and asks what you would do
                   ("Your ALB returns 503, what could be the reason?")
      coding     - write, trace or fix code
      project    - about THIS candidate's own project or resume
      followup   - only meaningful in the moment ("Which one?",
                   "That's on your localhost?", "Can you show me that?")
      behavioral - about the person, not the tech
  * is_reusable: TRUE only if another student, who was NOT in this room, could
    read the rewritten question on its own and practise answering it. Be
    strict - this decides whether it enters a bank shown to every student.
      REUSABLE: "Explain the event loop." / "How does JWT auth work?" /
                "Have you deployed an application?" / "Explain DB indexing."
      NOT:      "Show me your chatbot." / "Can you demo it?" /
                "Try it with Exotel." / "Open that file." / "Which one?"
    If answering it requires having watched this candidate's demo, seen their
    screen, or followed this conversation, it is NOT reusable.
    A reusable question NEVER contains: "show me", "can you show", "demo",
    "open that", "try it", or "your tool / your app / your project". If the
    rewrite still needs any of those, it is NOT reusable.
    A question can often be SAVED by generalising it away from this candidate:
      "Can you show me the knowledge base it crawled?"
        -> "How do you build and store a knowledge base for RAG?"  (reusable)
      "Does your AI SDR search for prospects on its own?"
        -> "How does an AI agent identify prospects automatically?" (reusable)
    Generalise first, then judge. If it cannot be generalised without the
    candidate's project, mark is_reusable false and move on.
  * model_answer: a short, correct, self-contained answer another student could
    learn from. Base it on the subject matter, NOT on what this candidate said.
  * why_asked: why an interviewer asks this, in plain language a student gets
    in five seconds. TWO LINES MAX. No jargon. Write "they want to know if you
    can build apps that stay reliable when lots of users hit them", NOT
    "evaluates distributed systems, fault tolerance, scalability".
  * prepare: 3-5 SHORT topics to revise before facing this question.
    e.g. ["Deployment", "Cloud hosting", "Web crawling", "DevOps basics"]
  * why_asked / model_answer / prepare: leave empty when is_reusable is false.
  * segment_order: the [number] where the question starts.

TASK 2 - REPORT for this candidate only. Do this only after TASK 1 is complete.
  * Cover EVERY question from TASK 1 that was put to this candidate - one entry
    in `answers` each, including ones they did not answer. Do not skip any.
  * answers[].question_text MUST be copied verbatim from the TASK 1
    question_text you already wrote (the rewritten form), so the two lists line
    up exactly.
  * Ground everything in the transcript. Never invent an answer.
  * When the candidate says "I don't know" / "no idea" / does not answer, set
    correctness "not_answered" and accuracy 0. Do not penalise them twice.
  * Judge the SUBSTANCE, not the transcription quality. Garbled words and
    filler are the recogniser's fault, not the candidate's - do not lower
    clarity for them. Judge clarity on structure and completeness of ideas.
  * student_answer: what they actually said, tidied into readable prose.
  * accuracy 0-100. ideal_answer: a brief correct answer they can learn from.
  * score 0-10 overall; verdict strong | average | weak.
  * improvements: specific and actionable, tied to what they actually got wrong.
  * skill_ratings: 1-5, ONLY for skills actually evidenced.
  * interviewer_feedback: if the interviewer gave the candidate verbal feedback
    or advice, quote it as closely as the transcript allows. Empty string if none.

This feedback is shown to the student. Be fair, specific and evidence-based.
"""

CATEGORY_ENUM = [
    "dsa", "python", "javascript", "react", "nodejs", "sql", "mongodb",
    "system_design", "devops", "docker", "kubernetes", "aws", "cloud", "cicd",
    "terraform", "linux", "networking", "security", "monitoring", "genai",
    "computer_vision", "project", "behavioral", "hr", "other",
]

# One candidate per call, so `report` is a single object rather than a list.
BASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions", "report"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "question_text", "raw_question_text", "category", "topic", "difficulty",
                    "is_technical", "question_type", "is_reusable", "model_answer",
                    "why_asked", "prepare", "segment_order",
                ],
                "properties": {
                    "question_text": {"type": "string"},
                    "raw_question_text": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORY_ENUM},
                    "topic": {"type": "string"},
                    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                    "is_technical": {"type": "boolean"},
                    "question_type": {
                        "type": "string",
                        "enum": ["conceptual", "scenario", "coding", "project", "followup", "behavioral"],
                    },
                    "is_reusable": {"type": "boolean"},
                    "model_answer": {"type": "string"},
                    "why_asked": {"type": "string"},
                    "prepare": {"type": "array", "items": {"type": "string"}},
                    "segment_order": {"type": "integer"},
                },
            },
        },
        "report": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "summary", "score", "verdict", "strengths", "improvements",
                "answers", "skill_ratings", "communication", "interviewer_feedback",
            ],
            "properties": {
                "summary": {"type": "string"},
                "score": {"type": "number"},
                "verdict": {"type": "string", "enum": ["strong", "average", "weak"]},
                "interviewer_feedback": {"type": "string"},
                "strengths": {"type": "array", "items": {"type": "string"}},
                "improvements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["area", "detail", "priority"],
                        "properties": {
                            "area": {"type": "string"},
                            "detail": {"type": "string"},
                            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        },
                    },
                },
                "answers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "question_text", "student_answer", "accuracy",
                            "correctness", "feedback", "ideal_answer",
                        ],
                        "properties": {
                            "question_text": {"type": "string"},
                            "student_answer": {"type": "string"},
                            "accuracy": {"type": "number"},
                            "correctness": {
                                "type": "string",
                                "enum": ["correct", "partial", "incorrect", "not_answered"],
                            },
                            "feedback": {"type": "string"},
                            "ideal_answer": {"type": "string"},
                        },
                    },
                },
                "skill_ratings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["skill", "rating"],
                        "properties": {
                            "skill": {"type": "string"},
                            "rating": {"type": "number"},
                        },
                    },
                },
                "communication": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["clarity", "confidence", "notes"],
                    "properties": {
                        "clarity": {"type": "number"},
                        "confidence": {"type": "number"},
                        "notes": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _strip_for_gemini(node: Any) -> Any:
    """Gemini's response_schema is an OpenAPI subset and rejects
    additionalProperties, so drop it while keeping type/enum/required."""
    if isinstance(node, dict):
        return {k: _strip_for_gemini(v) for k, v in node.items() if k != "additionalProperties"}
    if isinstance(node, list):
        return [_strip_for_gemini(item) for item in node]
    return node


GEMINI_SCHEMA = _strip_for_gemini(copy.deepcopy(BASE_SCHEMA))
OPENAI_SCHEMA = {"name": "interview_analysis", "strict": True, "schema": BASE_SCHEMA}


def build_user_prompt(*, transcript_text: str, student_label: str, context: dict[str, Any]) -> str:
    return (
        f"Company: {context.get('company') or 'Unknown'}\n"
        f"Role: {context.get('role') or 'Unknown'}\n"
        f"Interview round: {context.get('round_name') or 'Unknown'}\n"
        f"Interviewer speaker label: {context.get('interviewer') or 'Unknown'}\n\n"
        f"The CANDIDATE in this excerpt is the speaker labelled: {student_label}\n"
        "Every other speaker is the interviewer. Analyse only this candidate.\n\n"
        f"TRANSCRIPT:\n{transcript_text}"
    )


def _coerce(parsed: Any) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="AI returned a non-object response.")
    parsed.setdefault("questions", [])
    parsed.setdefault("report", {})
    return parsed


async def _analyze_openai(system: str, user: str) -> dict[str, Any]:
    settings = get_settings()
    key = (settings.openai_api_key or "").strip()
    # Tolerate a label accidentally pasted in front of the key in .env.
    if "sk-" in key and not key.startswith("sk-"):
        key = key[key.find("sk-"):]
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY is not configured.",
        )
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=key)
    try:
        response = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": OPENAI_SCHEMA},
            temperature=0.2,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"OpenAI request failed: {exc}") from exc

    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OpenAI returned an empty response.")
    try:
        parsed = _coerce(json.loads(content))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="OpenAI returned invalid JSON.") from exc
    parsed["_model"] = settings.openai_model
    return parsed


async def _analyze_gemini(system: str, user: str) -> dict[str, Any]:
    settings = get_settings()
    key = (settings.gemini_api_key or "").strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GEMINI_API_KEY is not configured. Add it to backend/.env to enable AI analysis.",
        )
    try:
        import google.generativeai as genai
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The 'google-generativeai' package is not installed.",
        ) from exc

    genai.configure(api_key=key)
    model = genai.GenerativeModel(settings.gemini_model, system_instruction=system)
    try:
        response = await model.generate_content_async(
            user,
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": GEMINI_SCHEMA,
                "temperature": 0.2,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Gemini request failed: {exc}") from exc

    content = (getattr(response, "text", "") or "").strip()
    if not content:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gemini returned an empty response.")
    try:
        parsed = _coerce(json.loads(content))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Gemini returned invalid JSON.") from exc
    parsed["_model"] = settings.gemini_model
    return parsed


async def analyze_candidate_block(
    *,
    transcript_text: str,
    student_label: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Analyse ONE candidate's slice of a transcript.

    Returns {"questions": [...], "report": {...}}. Raises HTTPException the
    caller can turn into ai_status='failed' with an actionable message.
    """
    settings = get_settings()
    truncated = transcript_text[: settings.ai_max_transcript_chars]
    user = build_user_prompt(transcript_text=truncated, student_label=student_label, context=context)

    provider = (settings.ai_provider or "gemini").strip().lower()
    if provider == "gemini":
        result = await _analyze_gemini(SYSTEM_PROMPT, user)
    elif provider == "openai":
        result = await _analyze_openai(SYSTEM_PROMPT, user)
    else:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"AI provider '{provider}' is not supported. Use 'gemini' or 'openai'.",
        )
    result["_truncated"] = len(transcript_text) > settings.ai_max_transcript_chars
    result["_provider"] = provider
    return result
