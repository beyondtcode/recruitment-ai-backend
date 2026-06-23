from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from crm_integration.config import get_notetaker_api_keys
from crm_integration.monday_client import execute_graphql
from crm_integration.schemas import NodeTakerWebhookPayload
from services.monday_service import normalize_email

logger = logging.getLogger(__name__)

ISR_TZ = ZoneInfo("Asia/Jerusalem")

NOTETAKER_API_VERSION = "2026-04"
NOTETAKER_PAGE_LIMIT = 100
NOTETAKER_ACCESS_LEVELS = ("ALL", "SHARED_WITH_ACCOUNT", "SHARED_WITH_ME")

NOTETAKER_MEETINGS_QUERY = """
query ($limit: Int!, $cursor: String, $filters: MeetingsFilterInput) {
  notetaker {
    meetings(limit: $limit, cursor: $cursor, filters: $filters) {
      meetings {
        id
        title
        start_time
        end_time
        summary
        participants {
          email
        }
        action_items {
          content
          is_completed
          owner
          due_date
        }
      }
      page_info {
        has_next_page
        cursor
      }
    }
  }
}
"""


def _parse_meeting_datetime(start_time: str | None) -> datetime | None:
    if not start_time:
        return None
    normalized = start_time.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        meeting_date = _parse_meeting_date(start_time)
        if meeting_date is None:
            return None
        return datetime.combine(meeting_date, datetime.min.time(), tzinfo=ISR_TZ)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ISR_TZ)
    return parsed.astimezone(ISR_TZ)


def _parse_meeting_date(start_time: str | None) -> date | None:
    if not start_time:
        return None
    normalized = start_time.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(normalized[:10])
    except ValueError:
        logger.warning("Could not parse notetaker start_time: %r", start_time)
        return None


def _participant_emails(meeting: dict[str, Any]) -> set[str]:
    emails: set[str] = set()
    for participant in meeting.get("participants") or []:
        if not isinstance(participant, dict):
            continue
        email = normalize_email(str(participant.get("email") or ""))
        if email:
            emails.add(email)
    return emails


def _meeting_dedupe_key(meeting: dict[str, Any]) -> str:
    meeting_id = str(meeting.get("id") or "").strip()
    if meeting_id:
        return f"id:{meeting_id}"

    title = str(meeting.get("title") or "").strip()
    start_time = str(meeting.get("start_time") or "").strip()
    return f"title_date:{title}|{start_time}"


def _meeting_matches_participants(
    meeting: dict[str, Any],
    email1: str,
    email2: str,
    target_date: date,
) -> bool:
    meeting_date = _parse_meeting_date(meeting.get("start_time"))
    if meeting_date != target_date:
        return False

    participants = _participant_emails(meeting)
    required = {normalize_email(email1), normalize_email(email2)}
    required.discard("")
    if len(required) < 2:
        return False
    return required.issubset(participants)


def _format_action_items(action_items: list[dict[str, Any]] | None) -> str:
    lines: list[str] = []
    for item in action_items or []:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        owner = str(item.get("owner") or "").strip()
        due_date = str(item.get("due_date") or "").strip()
        suffix_parts = [part for part in (owner, due_date) if part]
        if suffix_parts:
            content = f"{content} ({', '.join(suffix_parts)})"
        lines.append(f"- {content}")
    return "\n".join(lines)


def meeting_to_payload(meeting: dict[str, Any]) -> NodeTakerWebhookPayload:
    """Convert a notetaker.meetings record into the webhook payload shape."""
    meeting_date = _parse_meeting_date(meeting.get("start_time"))
    if meeting_date is None:
        raise ValueError(
            f"Notetaker meeting {meeting.get('title')!r} has no parseable start_time"
        )

    title = str(meeting.get("title") or "").strip()
    if not title:
        raise ValueError("Notetaker meeting is missing a title")

    participant_emails = sorted(_participant_emails(meeting))
    return NodeTakerWebhookPayload(
        meeting_title=title,
        meeting_date=meeting_date,
        participant_emails=participant_emails,
        meeting_summary=str(meeting.get("summary") or "").strip(),
        action_items=_format_action_items(meeting.get("action_items")),
    )


async def _fetch_notetaker_meeting_page(
    *,
    cursor: str | None = None,
    search: str | None = None,
    access: str = "ALL",
    api_key: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    filters: dict[str, Any] = {"access": access}
    if search:
        filters["search"] = search

    variables: dict[str, Any] = {
        "limit": NOTETAKER_PAGE_LIMIT,
        "filters": filters,
    }
    if cursor:
        variables["cursor"] = cursor

    body = await execute_graphql(
        NOTETAKER_MEETINGS_QUERY,
        variables,
        api_version=NOTETAKER_API_VERSION,
        api_key=api_key,
    )
    meetings_response = body.get("data", {}).get("notetaker", {}).get("meetings") or {}
    meetings = meetings_response.get("meetings") or []
    page_info = meetings_response.get("page_info") or {}
    next_cursor = page_info.get("cursor")
    has_next_page = bool(page_info.get("has_next_page"))
    return meetings, next_cursor, has_next_page


async def _fetch_all_notetaker_meetings(
    *,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """
    Fetch and merge Notetaker meetings across all configured API keys and access levels.
    """
    api_keys = get_notetaker_api_keys()
    merged: dict[str, dict[str, Any]] = {}

    logger.info(
        "Fetching notetaker meetings using %d API key(s)%s",
        len(api_keys),
        f" with search={search!r}" if search else "",
    )

    for key_index, api_key in enumerate(api_keys, start=1):
        key_count = 0
        for access in NOTETAKER_ACCESS_LEVELS:
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                meetings, next_cursor, has_next_page = await _fetch_notetaker_meeting_page(
                    cursor=cursor,
                    search=search,
                    access=access,
                    api_key=api_key,
                )
                for meeting in meetings:
                    dedupe_key = _meeting_dedupe_key(meeting)
                    if dedupe_key not in merged:
                        key_count += 1
                    merged[dedupe_key] = meeting

                if not has_next_page or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor

        logger.info(
            "Notetaker API key %d/%d contributed %d unique meeting(s)",
            key_index,
            len(api_keys),
            key_count,
        )

    logger.info("Notetaker fetch merged %d unique meeting(s) total", len(merged))
    return list(merged.values())


async def fetch_notetaker_meetings_since(
    since: datetime,
) -> list[NodeTakerWebhookPayload]:
    """
    Return all Notetaker meetings with start_time on or after ``since``.

    Paginates through notetaker.meetings and converts each match to a webhook payload.
    """
    since_local = since.astimezone(ISR_TZ)
    payloads: list[NodeTakerWebhookPayload] = []
    seen_keys: set[str] = set()

    logger.info(
        "Fetching notetaker meetings since %s",
        since_local.isoformat(),
    )

    meetings = await _fetch_all_notetaker_meetings()
    for meeting in meetings:
        meeting_dt = _parse_meeting_datetime(meeting.get("start_time"))
        if meeting_dt is None or meeting_dt < since_local:
            continue
        try:
            payload = meeting_to_payload(meeting)
        except ValueError as exc:
            logger.warning("Skipping notetaker meeting: %s", exc)
            continue
        dedupe_key = _meeting_dedupe_key(meeting)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        payloads.append(payload)

    logger.info("Fetched %d notetaker meetings since %s", len(payloads), since_local.isoformat())
    return payloads


async def fetch_meeting_by_participants(
    email1: str,
    email2: str,
    target_date: date,
) -> NodeTakerWebhookPayload | None:
    """
    Search Monday Notetaker meetings for one involving both participants on a given date.

    Uses the notetaker.meetings GraphQL query (API 2026-04+) and paginates until a match
    is found or all accessible meetings are exhausted.
    """
    normalized_emails = [normalize_email(email1), normalize_email(email2)]
    search_terms = [email for email in normalized_emails if email]

    logger.info(
        "Searching notetaker meetings for participants=%s date=%s",
        search_terms,
        target_date.isoformat(),
    )

    for search in [search_terms[0] if search_terms else None, None]:
        meetings = await _fetch_all_notetaker_meetings(search=search)
        for meeting in meetings:
            if _meeting_matches_participants(meeting, email1, email2, target_date):
                payload = meeting_to_payload(meeting)
                logger.info(
                    "Found notetaker meeting: title=%r date=%s participants=%s",
                    payload.meeting_title,
                    payload.meeting_date.isoformat(),
                    payload.participant_emails,
                )
                return payload

    logger.warning(
        "No notetaker meeting found for participants=%s on %s",
        search_terms,
        target_date.isoformat(),
    )
    return None
