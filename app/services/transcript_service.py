import difflib
import re
from datetime import datetime, timezone
from typing import Any

# "Sai Teja:", "Interviewer (00:12:03):", "00:12:03 Sai Teja:", "[00:12] Charan:"
LEADING_TIMESTAMP = re.compile(r"^\s*[\[\(]?\d{1,2}:\d{2}(?::\d{2})?[\]\)]?\s*[-–]?\s*")
# A standalone "00:03:16" block marker on its own line.
TIME_MARKER_LINE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s*$")
SPEAKER_LINE = re.compile(
    r"^\s*(?P<speaker>[A-Za-z][A-Za-z0-9 .'_\-]{0,59}?)"      # name
    r"(?:\s*[\[\(]\s*\d{1,2}:\d{2}(?::\d{2})?\s*[\]\)])?"      # optional inline timestamp
    r"\s*:\s*(?P<text>.*)$"
)
# Lines that look like "Note:", "http://x", "Topic: ..." are not speakers.
NON_SPEAKER_LABELS = {
    "note", "notes", "topic", "agenda", "http", "https", "date", "time",
    "meeting", "transcript", "summary", "attendees", "participants", "link",
}
# Google Meet's own footer/preamble. "Transcription ended after 01:01:44"
# otherwise parses as a speaker named "Transcription ended after 01".
BOILERPLATE_LINE = re.compile(
    r"^\s*(?:transcription\s+(?:ended|started)\b"
    r"|this\s+editable\s+transcript\b"
    r"|people\s+can\s+also\s+change\b"
    r"|.*\bcomputer\s+generated\b)",
    re.IGNORECASE,
)
# Pure meeting-logistics chatter carries no interview signal.
AV_NOISE = re.compile(
    r"^(?:am i audible|hello|hi|yes sir|okay sir|you're on mute|you are on mute"
    r"|can you hear me|is my audio clear|i'?ll be back|just give me \w+ minutes?)\b[\s.?!]*$",
    re.IGNORECASE,
)


def _clean_speaker(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip(" .-_")).strip()


def _is_speaker_label(label: str) -> bool:
    text = _clean_speaker(label)
    if not text or len(text) > 60:
        return False
    if text.lower() in NON_SPEAKER_LABELS:
        return False
    if "//" in text or "@" in text:
        return False
    # A speaker label is a short name, not a sentence.
    if len(text.split()) > 5:
        return False
    return True


def parse_transcript(raw_text: str) -> list[dict[str, Any]]:
    """Split a speaker-separated transcript into ordered segments.

    Consecutive lines from the same speaker are merged. Standalone "00:03:16"
    markers are recorded as the running timestamp for following segments rather
    than dropped, and Meet's own footer/preamble is skipped.
    """
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    time_marker: str | None = None

    for raw_line in (raw_text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if TIME_MARKER_LINE.match(line):
            time_marker = line.strip()
            current = None  # a new time block ends the previous run-on turn
            continue

        if BOILERPLATE_LINE.match(line):
            current = None
            continue

        candidate = LEADING_TIMESTAMP.sub("", line)
        match = SPEAKER_LINE.match(candidate)

        if match and _is_speaker_label(match.group("speaker")):
            speaker = _clean_speaker(match.group("speaker"))
            text = match.group("text").strip()
            if current and current["speaker"] == speaker:
                if text:
                    current["text"] = f"{current['text']} {text}".strip()
                continue
            current = {
                "order": len(segments),
                "speaker": speaker,
                "text": text,
                "at": time_marker,
            }
            segments.append(current)
            continue

        if current is not None:
            addition = candidate.strip()
            if addition:
                current["text"] = f"{current['text']} {addition}".strip()

    kept = [segment for segment in segments if segment["text"]]
    for index, segment in enumerate(kept):  # renumber after dropping empties
        segment["order"] = index
    return kept


def is_noise(segment: dict[str, Any]) -> bool:
    return bool(AV_NOISE.match((segment.get("text") or "").strip()))


def distinct_speakers(segments: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for segment in segments:
        if segment["speaker"] not in seen:
            seen.append(segment["speaker"])
    return seen


INTERVIEWER_HINTS = ("interviewer", "interview panel", "panel", "hr", "recruiter", "host", "moderator")
MATCH_THRESHOLD = 0.62


def _name_key(value: str | None) -> str:
    return re.sub(r"[^a-z]+", "", (value or "").lower())


def _tokens(value: str | None) -> set[str]:
    return {token for token in re.split(r"[^a-z]+", (value or "").lower()) if token}


def _match_score(speaker: str, student_name: str) -> float:
    """Score a transcript label against a student's full name.

    Plain fuzzy ratio is not enough: a transcript usually shows a short name
    ("Sai Teja") while the record holds the full name ("Sai Teja Garlapati"),
    which a raw ratio scores well below threshold. Containment and token-subset
    checks handle that case explicitly.
    """
    speaker_key, name_key = _name_key(speaker), _name_key(student_name)
    if not speaker_key or not name_key:
        return 0.0
    if speaker_key == name_key:
        return 1.0
    if speaker_key in name_key or name_key in speaker_key:
        return 0.95

    speaker_tokens, name_tokens = _tokens(speaker), _tokens(student_name)
    ratio = difflib.SequenceMatcher(None, speaker_key, name_key).ratio()
    if speaker_tokens and speaker_tokens <= name_tokens:
        return 0.92
    overlap = len(speaker_tokens & name_tokens)
    if overlap:
        return max(ratio, 0.6 + 0.1 * overlap)
    return ratio


def build_speaker_map(speakers: list[str], students: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort match of transcript speaker labels to the session's students.

    Assignment is globally greedy (best scoring pairs first) rather than
    first-come, so two students sharing a first name cannot steal each other's
    slot based purely on speaker order. A speaker that resembles no student is
    marked 'interviewer' when its label says so, else 'unknown' — an admin can
    correct it before analysis.
    """
    def student_id_of(student: dict[str, Any]) -> Any:
        return student.get("student_id") or student.get("_id")

    interviewers = {
        speaker for speaker in speakers
        if any(hint in speaker.lower() for hint in INTERVIEWER_HINTS)
    }

    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for speaker in speakers:
        if speaker in interviewers:
            continue
        for student in students:
            score = _match_score(speaker, student.get("name") or "")
            if score >= MATCH_THRESHOLD:
                candidates.append((score, speaker, student))
    candidates.sort(key=lambda item: item[0], reverse=True)

    resolved: dict[str, tuple[Any, float]] = {}
    taken_students: set[str] = set()
    for score, speaker, student in candidates:
        student_key = str(student_id_of(student))
        if speaker in resolved or student_key in taken_students:
            continue
        resolved[speaker] = (student_id_of(student), score)
        taken_students.add(student_key)

    mapping: list[dict[str, Any]] = []
    for speaker in speakers:
        if speaker in interviewers:
            mapping.append({"speaker_label": speaker, "student_id": None, "role": "interviewer", "confidence": 1.0})
        elif speaker in resolved:
            student_id, score = resolved[speaker]
            mapping.append(
                {
                    "speaker_label": speaker,
                    "student_id": student_id,
                    "role": "student",
                    "confidence": round(score, 2),
                }
            )
        else:
            mapping.append({"speaker_label": speaker, "student_id": None, "role": "unknown", "confidence": 0.0})
    return mapping


def transcript_to_text(segments: list[dict[str, Any]], *, limit: int | None = None) -> str:
    """Flatten segments back to speaker-prefixed text for the LLM prompt."""
    lines = [f"[{segment['order']}] {segment['speaker']}: {segment['text']}" for segment in segments]
    text = "\n".join(lines)
    if limit and len(text) > limit:
        return text[:limit]
    return text


# --- header ------------------------------------------------------------------

HEADER_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%d-%m-%Y")
# "Interviews | Nxtwave X WeSee  - Transcript" -> "WeSee"
HOST_ORGS = ("nxtwave", "niat")


def parse_header(raw_text: str) -> dict[str, Any]:
    """Pull the meeting date and the counterpart company out of Meet's header.

    Meet writes the date on one line and a title like
    "Interviews | Nxtwave X WeSee  - Transcript" on the next. The company we
    care about is the side that is not us.
    """
    lines = [line.strip() for line in (raw_text or "").splitlines()[:8] if line.strip()]
    meeting_date: datetime | None = None
    title: str | None = None

    for line in lines:
        if meeting_date is None:
            for fmt in HEADER_DATE_FORMATS:
                try:
                    meeting_date = datetime.strptime(line, fmt).replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue
            if meeting_date is not None:
                continue
        if title is None and "transcript" in line.lower():
            title = line

    company_hint = None
    if title:
        text = re.sub(r"-\s*transcript\s*$", "", title, flags=re.IGNORECASE)
        text = text.split("|")[-1]  # drop "Interviews |"
        parts = re.split(r"\s+[xX&]\s+|\s+and\s+", text)
        candidates = [
            part.strip(" -–—")
            for part in parts
            if part.strip() and not any(host in part.lower() for host in HOST_ORGS)
        ]
        company_hint = candidates[0] if candidates else None

    return {"meeting_date": meeting_date, "title": title, "company_hint": company_hint}


# --- roles & candidate blocks ------------------------------------------------


def detect_interviewer(segments: list[dict[str, Any]], student_speakers: set[str]) -> str | None:
    """Identify the interviewer by SPAN, not by name.

    Real interviewers are named ("Virendrasingh"), so a name heuristic fails.
    But the interviewer is the one non-student speaker present across the whole
    session while each candidate only occupies their own stretch.
    """
    others = [s for s in distinct_speakers(segments) if s not in student_speakers]
    if not others:
        return None

    total = len(segments)
    best, best_score = None, 0.0
    for speaker in others:
        orders = [s["order"] for s in segments if s["speaker"] == speaker]
        span = (orders[-1] - orders[0] + 1) / total if total else 0
        turns = len(orders) / total if total else 0
        questions = sum(1 for s in segments if s["speaker"] == speaker and "?" in (s["text"] or ""))
        score = span + turns + (questions / max(len(orders), 1))
        if score > best_score:
            best, best_score = speaker, score
    return best


def candidate_blocks(
    segments: list[dict[str, Any]],
    student_speakers: list[str],
    interviewer: str | None,
) -> list[dict[str, Any]]:
    """Split a multi-candidate transcript into one block per student.

    A single Meet recording often holds several back-to-back 1:1 interviews, so
    each student's block runs from their first to their last turn. Only that
    student and the interviewer are kept, which stops one candidate's answers
    leaking into another's report.
    """
    blocks: list[dict[str, Any]] = []
    for speaker in student_speakers:
        orders = [s["order"] for s in segments if s["speaker"] == speaker]
        if not orders:
            continue
        start, end = orders[0], orders[-1]
        keep = {speaker} | ({interviewer} if interviewer else set())
        block_segments = [
            s for s in segments
            if start <= s["order"] <= end and s["speaker"] in keep and not is_noise(s)
        ]
        blocks.append(
            {
                "speaker_label": speaker,
                "start_order": start,
                "end_order": end,
                "segment_count": len(block_segments),
                "segments": block_segments,
            }
        )
    blocks.sort(key=lambda block: block["start_order"])
    return blocks
