"""Route email CVs to per-job Monday boards via active position requests."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from services.monday_service import post_graphql

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"


def _ensure_backend_env() -> None:
    from dotenv import load_dotenv

    load_dotenv(_ENV_PATH)


POSITION_REQUESTS_BOARD_ID = "5099101115"
ACTIVE_RECRUITING_STATUS_INDEX = 1
POSITION_STATUS_COLUMN_ID = "color_mm4mk8nz"
POSITION_REQUIREMENTS_COLUMN_ID = "long_text_mm4mzfzg"
POSITION_RECRUITER_COLUMN_ID = "person"
ACTIVE_RECRUITING_STATUS_LABEL = "גיוס פעיל"
MATCH_SCORE_THRESHOLD = 60

POSITION_NAME_ALIASES: dict[str, str] = {
    "משרה עבור עופר": "משרה לעופר",
}

_ACTIVE_POSITIONS_QUERY = """
query ($boardId: [ID!]!, $limit: Int!) {
  boards(ids: $boardId) {
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        updated_at
        column_values(ids: [
          "color_mm4mk8nz",
          "long_text_mm4mzfzg",
          "person"
        ]) {
          id
          text
          value
          type
        }
      }
    }
  }
}
"""

_BOARDS_PAGE_QUERY = """
query ($limit: Int!, $page: Int!) {
  boards(limit: $limit, page: $page) {
    id
    name
  }
}
"""

_DRUSHIM_SUBJECT_RE = re.compile(
    r'קו["\u05f4]ח:\s*(.+?)\s*\|',
    re.IGNORECASE,
)
_HEBREW_APPLICATION_RE = re.compile(
    r"מועמדות\s+(?:לתפקיד|עבור\s+משרת)\s*[-:]?\s*(.+)",
    re.IGNORECASE,
)
_HEBREW_POSITION_RE = re.compile(r"משרה:\s*(.+)", re.IGNORECASE)
_ENGLISH_FOR_ROLE_RE = re.compile(
    r"(?:Nominee for|for)\s+(.+?)\s+(?:at|position)\b",
    re.IGNORECASE,
)
_HEBREW_FOR_ROLE_RE = re.compile(r"עבור\s+משרת\s+(.+)", re.IGNORECASE)
_CTO_TOKEN_RE = re.compile(r"\bCTO\b", re.IGNORECASE)
_TECH_BUSINESS_LEAD_RE = re.compile(
    r"Technology\s+Business(?:es)?\s+Lead",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ActivePosition:
    item_id: str
    item_name: str
    requirements_text: str
    recruiter_people_text: str
    updated_at: str
    board_id: str | None = None
    board_name: str | None = None


@dataclass(frozen=True)
class PositionMatch:
    position: ActivePosition
    board_id: str
    score: int
    matched_hint: str


@dataclass(frozen=True)
class RoutingContext:
    active_positions: tuple[ActivePosition, ...]
    board_name_to_id: dict[str, str]
    board_id_to_name: dict[str, str] | None = None


def get_position_requests_board_id() -> str:
    return POSITION_REQUESTS_BOARD_ID


def get_active_recruiting_status_index() -> int:
    return ACTIVE_RECRUITING_STATUS_INDEX


def normalize_position_name(name: str) -> str:
    """Normalize a position/board name for comparison."""
    text = unicodedata.normalize("NFKC", name or "")
    text = text.strip().casefold()
    text = re.sub(r"[^\w\s\u0590-\u05ff+#]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"משרה\s+עבור\s+", "משרה ל", text)
    return text


def _status_index(column: dict[str, Any]) -> int | None:
    raw = column.get("value")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    index = parsed.get("index")
    return int(index) if index is not None else None


def _column_text(item: dict[str, Any], column_id: str) -> str:
    for column in item.get("column_values") or []:
        if str(column.get("id")) == column_id:
            return str(column.get("text") or "").strip()
    return ""


def _is_active_recruiting_item(item: dict[str, Any], *, status_index: int) -> bool:
    for column in item.get("column_values") or []:
        if str(column.get("id")) != POSITION_STATUS_COLUMN_ID:
            continue
        index = _status_index(column)
        if index is not None:
            return index == status_index
        return str(column.get("text") or "").strip() == ACTIVE_RECRUITING_STATUS_LABEL
    return False


def _parse_active_position(item: dict[str, Any]) -> ActivePosition:
    return ActivePosition(
        item_id=str(item.get("id") or ""),
        item_name=str(item.get("name") or "").strip(),
        requirements_text=_column_text(item, POSITION_REQUIREMENTS_COLUMN_ID),
        recruiter_people_text=_column_text(item, POSITION_RECRUITER_COLUMN_ID),
        updated_at=str(item.get("updated_at") or ""),
    )


async def fetch_active_recruiting_positions(
    board_id: str | None = None,
    *,
    status_index: int | None = None,
    limit: int = 500,
) -> list[ActivePosition]:
    """Return items on the position-requests board with active recruiting status."""
    board_key = board_id or get_position_requests_board_id()
    target_index = status_index if status_index is not None else get_active_recruiting_status_index()

    body = await post_graphql(
        _ACTIVE_POSITIONS_QUERY,
        {"boardId": [board_key], "limit": limit},
        column_ids=[
            POSITION_STATUS_COLUMN_ID,
            POSITION_REQUIREMENTS_COLUMN_ID,
            POSITION_RECRUITER_COLUMN_ID,
        ],
    )
    boards = body.get("data", {}).get("boards") or []
    if not boards:
        logger.warning("Position requests board %s not found", board_key)
        return []

    items = (boards[0].get("items_page") or {}).get("items") or []
    active = [
        _parse_active_position(item)
        for item in items
        if _is_active_recruiting_item(item, status_index=target_index)
    ]
    logger.info(
        "Loaded %d active recruiting position(s) from board %s",
        len(active),
        board_key,
    )
    return active


async def fetch_all_boards(*, page_size: int = 500) -> list[dict[str, str]]:
    """Paginate through workspace boards and return id/name pairs."""
    boards: list[dict[str, str]] = []
    page = 1
    while True:
        body = await post_graphql(
            _BOARDS_PAGE_QUERY,
            {"limit": page_size, "page": page},
        )
        batch = body.get("data", {}).get("boards") or []
        if not batch:
            break
        for board in batch:
            board_id = str(board.get("id") or "").strip()
            board_name = str(board.get("name") or "").strip()
            if board_id and board_name:
                boards.append({"id": board_id, "name": board_name})
        if len(batch) < page_size:
            break
        page += 1
    return boards


def build_board_name_index(boards: list[dict[str, str]]) -> dict[str, str]:
    """Map normalized board name -> board id (last wins on duplicate names)."""
    index: dict[str, str] = {}
    for board in boards:
        normalized = normalize_position_name(board["name"])
        if normalized:
            index[normalized] = board["id"]
    return index


def build_board_id_to_name(boards: list[dict[str, str]]) -> dict[str, str]:
    """Map board id -> display name."""
    return {
        str(board["id"]): str(board["name"])
        for board in boards
        if board.get("id") and board.get("name")
    }


def _resolve_board_for_position(
    position: ActivePosition,
    board_name_to_id: dict[str, str],
    board_id_to_name: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    candidates = [position.item_name]
    alias = POSITION_NAME_ALIASES.get(position.item_name)
    if alias:
        candidates.append(alias)

    for candidate in candidates:
        normalized = normalize_position_name(candidate)
        board_id = board_name_to_id.get(normalized)
        if board_id:
            board_name = (board_id_to_name or {}).get(board_id, candidate)
            return board_id, board_name

    normalized_item = normalize_position_name(position.item_name)
    for board_name, board_id in board_name_to_id.items():
        if normalized_item in board_name or board_name in normalized_item:
            display_name = (board_id_to_name or {}).get(board_id, board_name)
            return board_id, display_name

    return None, None


def resolve_positions_with_boards(
    positions: list[ActivePosition],
    board_name_to_id: dict[str, str],
    board_id_to_name: dict[str, str] | None = None,
) -> list[ActivePosition]:
    """Attach board_id/board_name to each active position when resolvable."""
    resolved: list[ActivePosition] = []
    for position in positions:
        board_id, board_name = _resolve_board_for_position(
            position,
            board_name_to_id,
            board_id_to_name,
        )
        if board_id is None:
            logger.warning(
                "No Monday board found for active position %r (item %s)",
                position.item_name,
                position.item_id,
            )
        resolved.append(
            ActivePosition(
                item_id=position.item_id,
                item_name=position.item_name,
                requirements_text=position.requirements_text,
                recruiter_people_text=position.recruiter_people_text,
                updated_at=position.updated_at,
                board_id=board_id,
                board_name=board_name,
            )
        )
    return resolved


async def load_routing_context() -> RoutingContext:
    """Load active positions and board-name index for one email batch run."""
    _ensure_backend_env()
    positions, boards = await asyncio.gather(
        fetch_active_recruiting_positions(),
        fetch_all_boards(),
    )
    board_name_to_id = build_board_name_index(boards)
    board_id_to_name = build_board_id_to_name(boards)
    resolved = resolve_positions_with_boards(
        positions,
        board_name_to_id,
        board_id_to_name,
    )
    return RoutingContext(
        active_positions=tuple(resolved),
        board_name_to_id=board_name_to_id,
        board_id_to_name=board_id_to_name,
    )


def extract_position_hint(subject: str, body_snippet: str = "") -> str | None:
    """Extract a position/job-title hint from email subject (and optional body)."""
    subject = (subject or "").strip()
    body_snippet = (body_snippet or "").strip()
    combined = f"{subject}\n{body_snippet}".strip()
    if not combined:
        return None

    for pattern in (
        _DRUSHIM_SUBJECT_RE,
        _HEBREW_APPLICATION_RE,
        _HEBREW_POSITION_RE,
        _HEBREW_FOR_ROLE_RE,
        _ENGLISH_FOR_ROLE_RE,
    ):
        match = pattern.search(subject) or pattern.search(combined)
        if match:
            hint = match.group(1).strip(" -:|")
            if hint:
                return hint

    if _CTO_TOKEN_RE.search(combined):
        return "CTO"

    tech_lead = _TECH_BUSINESS_LEAD_RE.search(combined)
    if tech_lead:
        return tech_lead.group(0)

    return None


def _token_set(text: str) -> set[str]:
    normalized = normalize_position_name(text)
    return {token for token in normalized.split() if len(token) > 1}


def _score_match(hint: str, position: ActivePosition) -> int:
    normalized_hint = normalize_position_name(hint)
    if not normalized_hint:
        return 0

    name_candidates = [position.item_name]
    if position.board_name:
        name_candidates.append(position.board_name)
    alias = POSITION_NAME_ALIASES.get(position.item_name)
    if alias:
        name_candidates.append(alias)

    best = 0
    for candidate in name_candidates:
        normalized_candidate = normalize_position_name(candidate)
        if not normalized_candidate:
            continue
        if normalized_hint == normalized_candidate:
            best = max(best, 100)
        elif normalized_hint in normalized_candidate or normalized_candidate in normalized_hint:
            best = max(best, 90)

    requirements = normalize_position_name(position.requirements_text)
    if requirements and normalized_hint in requirements:
        best = max(best, 80)

    hint_tokens = _token_set(hint)
    if hint_tokens:
        for candidate in name_candidates:
            overlap = hint_tokens & _token_set(candidate)
            if len(overlap) >= 2 or (
                len(overlap) == 1 and len(hint_tokens) == 1
            ):
                best = max(best, 60)

    return best


def match_email_to_position(
    subject: str,
    body_snippet: str,
    active_positions: tuple[ActivePosition, ...] | list[ActivePosition],
    *,
    board_name_to_id: dict[str, str] | None = None,
) -> PositionMatch | None:
    """
    Match an email to an active position and its job board.

    Returns None when no confident match is found (caller should use Main Hub).
    """
    if not active_positions:
        return None

    hints: list[str] = []
    extracted = extract_position_hint(subject, body_snippet)
    if extracted:
        hints.append(extracted)
    if subject.strip():
        hints.append(subject.strip())

    best_match: PositionMatch | None = None
    for hint in hints:
        for position in active_positions:
            if not position.board_id:
                continue
            score = _score_match(hint, position)
            if score < MATCH_SCORE_THRESHOLD:
                continue
            candidate = PositionMatch(
                position=position,
                board_id=position.board_id,
                score=score,
                matched_hint=hint,
            )
            if best_match is None:
                best_match = candidate
                continue
            if score > best_match.score:
                best_match = candidate
            elif score == best_match.score:
                if position.updated_at > best_match.position.updated_at:
                    best_match = candidate

    if best_match:
        logger.info(
            "Matched email subject %r to position %r (board %s, score %d)",
            subject,
            best_match.position.item_name,
            best_match.board_id,
            best_match.score,
        )
    return best_match


async def resolve_job_requirements_for_match(match: PositionMatch) -> str | None:
    """Prefer job-board mirror requirements; fall back to request-board item text."""
    from services.monday_service import fetch_board_job_requirements

    requirements = await fetch_board_job_requirements(match.board_id)
    if requirements:
        return requirements
    text = (match.position.requirements_text or "").strip()
    return text or None


def _fetch_recent_cv_email_subjects(*, lookback_days: int = 7, limit: int = 30) -> list[dict[str, str]]:
    """Read-only IMAP scan for recent CV email metadata (no attachment download)."""
    import datetime as dt

    from dotenv import load_dotenv
    from imap_tools import AND, MailBox

    load_dotenv(_ENV_PATH)
    host = os.getenv("EMAIL_HOST")
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    if not host or not user or not password:
        raise ValueError("EMAIL_HOST, EMAIL_USER, and EMAIL_PASSWORD must be set in .env")

    today = dt.date.today()
    since = today - dt.timedelta(days=lookback_days)
    results: list[dict[str, str]] = []

    with MailBox(host).login(user, password, initial_folder="INBOX") as mailbox:
        for msg in mailbox.fetch(AND(date_gte=since), limit=limit, reverse=True):
            cv_files = [
                att.filename
                for att in msg.attachments
                if att.filename and Path(att.filename).suffix.lower() in {".pdf", ".docx"}
            ]
            if not cv_files:
                continue
            results.append(
                {
                    "uid": str(msg.uid),
                    "subject": (msg.subject or "").strip(),
                    "from": (msg.from_ or "").strip(),
                    "files": ", ".join(cv_files[:3]),
                }
            )
    return results


async def debug_match_recent(*, lookback_days: int = 7, limit: int = 30) -> list[dict[str, Any]]:
    """
    Dry-run helper: print subject -> matched position -> board_id for recent CV emails.

    Does not download attachments or write to Monday.
    """
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    context = await load_routing_context()
    emails = await asyncio.to_thread(
        _fetch_recent_cv_email_subjects,
        lookback_days=lookback_days,
        limit=limit,
    )

    rows: list[dict[str, Any]] = []
    print(f"\nActive positions ({len(context.active_positions)}):")
    for position in context.active_positions:
        board_label = position.board_id or "NO BOARD"
        print(f"  - {position.item_name!r} -> board {board_label}")

    print(f"\nRecent CV emails ({len(emails)}):")
    for email in emails:
        match = match_email_to_position(
            email["subject"],
            "",
            context.active_positions,
            board_name_to_id=context.board_name_to_id,
        )
        row = {
            "uid": email["uid"],
            "subject": email["subject"],
            "from": email["from"],
            "files": email["files"],
            "matched_position": match.position.item_name if match else None,
            "board_id": match.board_id if match else None,
            "score": match.score if match else None,
            "matched_hint": match.matched_hint if match else None,
        }
        rows.append(row)
        if match:
            print(
                f"  UID {email['uid']}: {email['subject']!r}\n"
                f"    -> {match.position.item_name!r} (board {match.board_id}, score {match.score})"
            )
        else:
            print(f"  UID {email['uid']}: {email['subject']!r}\n    -> Main Hub only (no match)")

    return rows
