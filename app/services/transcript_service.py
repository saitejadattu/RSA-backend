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

HEADER_DATE_FORMATS = (
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d-%m-%Y",
    "%b %d, %Y, %I:%M %p",
    "%B %d, %Y, %I:%M %p",
    "%b %d, %Y %H:%M",
    "%B %d, %Y %H:%M",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%B %d %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%d %b, %Y",
    "%d %B, %Y",
    "%d-%b-%Y",
    "%d-%B-%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
    "%m-%d-%Y",
)
# "Interviews | Nxtwave X WeSee  - Transcript" -> "WeSee"
HOST_ORGS = ("nxtwave", "niat")


def extract_header_date(line: str) -> datetime | None:
    """Extract and parse meeting date from a header line with various formats."""
    return extract_date_from_text(line)


def extract_date_from_text(text: str | None) -> datetime | None:
    """Extract an interview date from arbitrary text (title, company_hint, raw_text, etc.)."""
    if not text or not isinstance(text, str):
        return None

    # 1. Regex pattern for YYYY/MM/DD or YYYY-MM-DD (with optional HH:MM[:SS])
    # e.g., "2026/06/30 14:56 IST", "2026-07-29 15:57", "2026/07/29"
    ymd_pattern = re.search(
        r"\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[\s,T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b",
        text,
    )
    if ymd_pattern:
        try:
            year = int(ymd_pattern.group(1))
            month = int(ymd_pattern.group(2))
            day = int(ymd_pattern.group(3))
            hour = int(ymd_pattern.group(4)) if ymd_pattern.group(4) is not None else 0
            minute = int(ymd_pattern.group(5)) if ymd_pattern.group(5) is not None else 0
            second = int(ymd_pattern.group(6)) if ymd_pattern.group(6) is not None else 0
            if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass

    # 2. Regex pattern for DD/MM/YYYY or DD-MM-YYYY (with optional HH:MM[:SS])
    # e.g., "30/06/2026 14:56", "29-07-2026"
    dmy_pattern = re.search(
        r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:[\s,T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?\b",
        text,
    )
    if dmy_pattern:
        try:
            day = int(dmy_pattern.group(1))
            month = int(dmy_pattern.group(2))
            year = int(dmy_pattern.group(3))
            hour = int(dmy_pattern.group(4)) if dmy_pattern.group(4) is not None else 0
            minute = int(dmy_pattern.group(5)) if dmy_pattern.group(5) is not None else 0
            second = int(dmy_pattern.group(6)) if dmy_pattern.group(6) is not None else 0
            if month > 12 and 1 <= day <= 12:
                day, month = month, day
            if 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        except (ValueError, OverflowError):
            pass

    # 3. Month name patterns: "July 29, 2026", "29 July 2026", "Jul 29 2026 15:57"
    month_names = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|May|June|July|August|September|October|November|December"
    m_name_1 = re.search(
        rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?:[\s,T]+(\d{{1,2}}):(\d{{2}}))?",
        text,
        re.IGNORECASE,
    )
    if m_name_1:
        try:
            m_str, d_str, y_str = m_name_1.group(1), m_name_1.group(2), m_name_1.group(3)
            hour = int(m_name_1.group(4)) if m_name_1.group(4) is not None else 0
            minute = int(m_name_1.group(5)) if m_name_1.group(5) is not None else 0
            parsed_dt = datetime.strptime(f"{m_str} {d_str} {y_str}", "%b %d %Y" if len(m_str) <= 3 else "%B %d %Y")
            return parsed_dt.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    m_name_2 = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s-]+({month_names})[\s-,]+(\d{{4}})(?:[\s,T]+(\d{{1,2}}):(\d{{2}}))?",
        text,
        re.IGNORECASE,
    )
    if m_name_2:
        try:
            d_str, m_str, y_str = m_name_2.group(1), m_name_2.group(2), m_name_2.group(3)
            hour = int(m_name_2.group(4)) if m_name_2.group(4) is not None else 0
            minute = int(m_name_2.group(5)) if m_name_2.group(5) is not None else 0
            parsed_dt = datetime.strptime(f"{d_str} {m_str} {y_str}", "%d %b %Y" if len(m_str) <= 3 else "%d %B %Y")
            return parsed_dt.replace(hour=hour, minute=minute, tzinfo=timezone.utc)
        except ValueError:
            pass

    # 4. Check first few lines individually
    lines = [line.strip() for line in text.splitlines()[:10] if line.strip()]
    for line in lines:
        d = extract_header_date(line)
        if d is not None:
            return d

    return None


def resolve_interview_date(
    report: dict | None = None,
    session: dict | None = None,
    transcript: dict | None = None,
) -> datetime | str | None:
    """Resolve the interview occurrence date following the strict 9-tier priority:
    1. interview_date (on report or session)
    2. scheduled_at (on session or report)
    3. started_at (on session or report)
    4. meeting_date (on transcript, session, or report)
    5. Date extracted from transcript title
    6. Date extracted from transcript company_hint
    7. Date extracted from transcript raw_text
    8. generated_at (on report)
    9. created_at (on report, session, or transcript)

    Never falls back to 'now'.
    """
    report = report or {}
    session = session or report.get("session") or report.get("sess") or {}
    transcript = transcript or report.get("transcript") or report.get("tr") or {}

    # 1. interview_date
    val = report.get("interview_date") or session.get("interview_date") or transcript.get("interview_date")
    if val:
        return val

    # 2. scheduled_at
    val = session.get("scheduled_at") or report.get("scheduled_at")
    if val:
        return val

    # 3. started_at
    val = session.get("started_at") or report.get("started_at")
    if val:
        return val

    # 4. meeting_date
    val = transcript.get("meeting_date") or session.get("meeting_date") or report.get("meeting_date")
    if val:
        return val

    # 5. Date extracted from transcript title
    title = transcript.get("title") or session.get("title") or report.get("title")
    if title:
        d = extract_date_from_text(title)
        if d:
            return d

    # 6. Date extracted from company_hint
    company_hint = transcript.get("company_hint") or session.get("company_hint") or report.get("company_hint")
    if company_hint:
        d = extract_date_from_text(company_hint)
        if d:
            return d

    # 7. Date extracted from transcript raw_text
    raw_text = transcript.get("raw_text") or session.get("raw_text") or report.get("raw_text")
    if raw_text:
        d = extract_date_from_text(raw_text)
        if d:
            return d

    # 8. generated_at
    val = report.get("generated_at") or session.get("generated_at")
    if val:
        return val

    # 9. created_at
    val = report.get("created_at") or session.get("created_at") or transcript.get("created_at")
    if val:
        return val

    return None


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
        if title is None and "transcript" in line.lower():
            title = line
        if meeting_date is None:
            meeting_date = extract_header_date(line)

    # If meeting_date is not found yet, check if title or raw_text contains the date
    if meeting_date is None and title:
        meeting_date = extract_date_from_text(title)
    if meeting_date is None:
        meeting_date = extract_date_from_text(raw_text)

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
        if candidates:
            # Strip embedded date from company hint, e.g. "GTMER - 2026/06/30 14:56 IST" -> "GTMER"
            hint = candidates[0]
            hint_cleaned = re.sub(r"\s*-\s*\d{4}[/-]\d{1,2}[/-]\d{1,2}.*$", "", hint).strip()
            company_hint = hint_cleaned if hint_cleaned else hint

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
