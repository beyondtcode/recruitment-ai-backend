from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.meeting import (
    column_text,
    external_participant_emails,
    gather_past_meeting_context,
    meeting_already_exists,
    parse_comma_separated_emails,
    status_column_index,
)
from crm_integration.monday_client import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    FIND_ITEMS_LIMIT,
    ITEMS_PAGE_BY_COLUMN_VALUES_WITH_COLUMNS_QUERY,
    execute_graphql,
)
from crm_integration.lookup import find_contact_by_emails
from crm_integration.monday_fetcher import ISR_TZ, fetch_notetaker_meetings_since
from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.schemas import NodeTakerWebhookResult
from services.ai_service import generate_meeting_brief

logger = logging.getLogger(__name__)

NOTETAKER_BATCH_DELAY_SECONDS = 300
_notetaker_batch_task: asyncio.Task[None] | None = None

NEW_MEETING_STATUS_INDEX = 0
NEW_MEETING_STATUS_LABEL = "פגישה חדשה"
BRIEF_SENT_STATUS_INDEX = 1


def _column_by_id(item: dict[str, Any], column_id: str) -> dict[str, Any] | None:
    for column in item.get("column_values") or []:
        if str(column.get("id")) == column_id:
            return column
    return None


def _is_new_meeting_item(item: dict[str, Any], status_column_id: str) -> bool:
    status_column = _column_by_id(item, status_column_id)
    if not status_column:
        return False

    index = status_column_index(status_column)
    if index is not None:
        return index == NEW_MEETING_STATUS_INDEX

    return column_text(status_column) == NEW_MEETING_STATUS_LABEL


async def _fetch_todays_future_meetings(
    settings: CrmSettings,
    today_iso: str,
) -> list[dict[str, Any]]:
    body = await execute_graphql(
        ITEMS_PAGE_BY_COLUMN_VALUES_WITH_COLUMNS_QUERY,
        {
            "boardId": settings.future_meetings_board_id,
            "limit": FIND_ITEMS_LIMIT,
            "columns": [
                {
                    "column_id": settings.future_meetings_date_column_id,
                    "column_values": [today_iso],
                }
            ],
            "columnIds": [
                settings.future_meetings_status_column_id,
                settings.future_meetings_participants_column_id,
            ],
        },
        column_ids=[
            settings.future_meetings_date_column_id,
            settings.future_meetings_status_column_id,
            settings.future_meetings_participants_column_id,
        ],
    )
    items = body.get("data", {}).get("items_page_by_column_values", {}).get("items") or []
    if len(items) >= FIND_ITEMS_LIMIT:
        logger.warning(
            "Future meetings query hit FIND_ITEMS_LIMIT=%d for date %s",
            FIND_ITEMS_LIMIT,
            today_iso,
        )
    return [
        item
        for item in items
        if _is_new_meeting_item(item, settings.future_meetings_status_column_id)
    ]


async def _update_future_meeting_brief(
    item_id: str,
    brief_text: str,
    settings: CrmSettings,
) -> None:
    column_values = {
        settings.future_meetings_brief_column_id: brief_text,
        settings.future_meetings_status_column_id: {"index": BRIEF_SENT_STATUS_INDEX},
    }
    await execute_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": settings.future_meetings_board_id,
            "itemId": item_id,
            "columnValues": json.dumps(column_values),
        },
        column_ids=list(column_values.keys()),
    )


async def process_morning_briefs(
    settings: CrmSettings | None = None,
) -> dict[str, object]:
    """
    Generate and write morning preparation briefs for today's new future meetings.
    """
    settings = settings or get_crm_settings()
    today = datetime.now(ISR_TZ).date()
    today_iso = today.isoformat()

    meetings = await _fetch_todays_future_meetings(settings, today_iso)

    processed: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for item in meetings:
        item_id = str(item.get("id") or "")
        title = str(item.get("name") or "").strip()
        meeting_key = {"item_id": item_id, "title": title, "date": today_iso}

        participants_column = _column_by_id(
            item,
            settings.future_meetings_participants_column_id,
        )
        raw_participants = column_text(participants_column or {})
        emails = external_participant_emails(parse_comma_separated_emails(raw_participants))

        if not emails:
            logger.warning(
                "Skipping morning brief for item %s (%r): no external participant emails",
                item_id,
                title,
            )
            skipped.append({**meeting_key, "reason": "no_external_participants"})
            continue

        try:
            match = await find_contact_by_emails(emails, settings=settings)
            if not match:
                logger.warning(
                    "Skipping morning brief for item %s (%r): no CRM client/lead match for %s",
                    item_id,
                    title,
                    emails,
                )
                skipped.append({**meeting_key, "reason": "no_crm_contact_match"})
                continue

            past_context = await gather_past_meeting_context(
                emails,
                before_date=today,
                settings=settings,
            )
            if not past_context.strip():
                logger.warning(
                    "Skipping morning brief for item %s (%r): no past meeting history for %s",
                    item_id,
                    title,
                    emails,
                )
                skipped.append({**meeting_key, "reason": "no_past_meeting_context"})
                continue

            brief = await generate_meeting_brief(
                past_context,
                title,
                participant_emails=emails,
            )
            await _update_future_meeting_brief(item_id, brief, settings)
            processed.append(
                {
                    **meeting_key,
                    "participant_emails": emails,
                    "match_type": match.match_type,
                    "matched_email": match.matched_email,
                }
            )
        except Exception as exc:
            logger.exception(
                "Failed to process morning brief for item %s title=%r",
                item_id,
                title,
            )
            errors.append({**meeting_key, "error": str(exc)})

    summary = {
        "date": today_iso,
        "fetched": len(meetings),
        "processed_count": len(processed),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info(
        "Morning briefing batch complete: fetched=%d processed=%d skipped=%d errors=%d",
        len(meetings),
        len(processed),
        len(skipped),
        len(errors),
    )
    return summary


def notetaker_batch_run_at(now: datetime | None = None) -> datetime:
    """Return the next 00:05 Asia/Jerusalem run time for the nightly batch."""
    current = (now or datetime.now(ISR_TZ)).astimezone(ISR_TZ)
    target = current.replace(hour=0, minute=5, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


def notetaker_batch_delay_seconds(now: datetime | None = None) -> float:
    """Seconds until the batch should run (00:05 ISR, or 5 minutes if already past)."""
    current = (now or datetime.now(ISR_TZ)).astimezone(ISR_TZ)
    target = current.replace(hour=0, minute=5, second=0, microsecond=0)
    if current < target:
        return (target - current).total_seconds()
    return float(NOTETAKER_BATCH_DELAY_SECONDS)


async def run_daily_notetaker_batch() -> dict[str, object]:
    logger.info("Daily notetaker batch started")
    try:
        summary = await process_recent_notetaker_meetings(hours=24)
        logger.info(
            "Daily notetaker batch finished: processed=%d skipped=%d errors=%d",
            summary["processed_count"],
            summary["skipped_count"],
            summary["error_count"],
        )
        return summary
    except Exception:
        logger.exception("Daily notetaker batch failed")
        raise


async def _run_notetaker_batch_after_delay(delay_seconds: float) -> None:
    await asyncio.sleep(delay_seconds)
    await run_daily_notetaker_batch()


def schedule_notetaker_batch(
    now: datetime | None = None,
) -> tuple[datetime, Literal["scheduled", "already_scheduled"], float]:
    """
    Schedule the nightly Notetaker batch (idempotent until the current run completes).

    Intended flow: external webhook at 00:00 wakes the server; batch runs at 00:05 ISR.
    """
    global _notetaker_batch_task

    current = (now or datetime.now(ISR_TZ)).astimezone(ISR_TZ)
    run_at = notetaker_batch_run_at(current)
    delay_seconds = notetaker_batch_delay_seconds(current)

    if _notetaker_batch_task is not None and not _notetaker_batch_task.done():
        logger.info(
            "Notetaker batch already scheduled; ignoring duplicate webhook (next run %s)",
            run_at.isoformat(),
        )
        return run_at, "already_scheduled", delay_seconds

    logger.info(
        "Notetaker batch scheduled via webhook: runs at %s (in %.0f seconds)",
        run_at.isoformat(),
        delay_seconds,
    )
    _notetaker_batch_task = asyncio.create_task(_run_notetaker_batch_after_delay(delay_seconds))
    return run_at, "scheduled", delay_seconds


async def process_recent_notetaker_meetings(
    hours: int = 24,
    settings: CrmSettings | None = None,
) -> dict[str, object]:
    """
    Fetch Notetaker meetings from the past ``hours`` and run the CRM pipeline for each new one.
    """
    settings = settings or get_crm_settings()
    since = datetime.now(ISR_TZ) - timedelta(hours=hours)
    payloads = await fetch_notetaker_meetings_since(since)

    processed: list[dict[str, object]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for payload in payloads:
        meeting_key = {
            "title": payload.meeting_title,
            "date": payload.meeting_date.isoformat(),
        }
        try:
            if await meeting_already_exists(payload, settings=settings):
                skipped.append(meeting_key)
                continue
            result: NodeTakerWebhookResult = await process_nodetaker_webhook(
                payload,
                settings=settings,
            )
            processed.append(
                {
                    **meeting_key,
                    "meeting_item_id": result.meeting_item_id,
                    "doc_created": result.doc_created,
                }
            )
        except Exception as exc:
            logger.exception(
                "Failed to process notetaker meeting title=%r date=%s",
                payload.meeting_title,
                payload.meeting_date.isoformat(),
            )
            errors.append({**meeting_key, "error": str(exc)})

    summary = {
        "since": since.isoformat(),
        "fetched": len(payloads),
        "processed_count": len(processed),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
    }
    logger.info(
        "Notetaker batch complete: fetched=%d processed=%d skipped=%d errors=%d",
        len(payloads),
        len(processed),
        len(skipped),
        len(errors),
    )
    return summary
