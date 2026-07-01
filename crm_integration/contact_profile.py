from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from crm_integration.config import CrmSettings
from crm_integration.lookup import ContactMatch
from crm_integration.meeting import column_text
from crm_integration.monday_client import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    ITEMS_BY_IDS_QUERY,
    execute_graphql,
)

logger = logging.getLogger(__name__)

CONTACT_PROFILE_COLUMNS: dict[str, dict[str, str]] = {
    "client": {
        "profile_column_id": "text_mm4mqp9k",
        "latest_date_column_id": "date_mm4d7cs2",
        "progress_column_id": "long_text_mm4vgz0h",
    },
    "lead": {
        "profile_column_id": "text_mm4jajn2",
        "latest_date_column_id": "date_mm4ddb0d",
        "progress_column_id": "long_text_mm4vb3t3",
    },
}


def _column_value_for_text(column_id: str, text: str) -> str | dict[str, str]:
    if column_id.startswith("long_text_"):
        return {"text": text}
    return text


def _date_column_value(iso_date: str) -> dict[str, str]:
    return {"date": iso_date}


def _board_id_for_match(match: ContactMatch, settings: CrmSettings) -> str:
    if match.match_type == "client":
        return settings.monday_crm_active_clients_board_id
    if match.match_type == "lead":
        return settings.monday_crm_leads_board_id
    raise ValueError(f"Unsupported match type: {match.match_type!r}")


def format_progress_date(meeting_date: date) -> str:
    """Format meeting date as D.M.YY (e.g. 1.1.26)."""
    return f"{meeting_date.day}.{meeting_date.month}.{meeting_date.year % 100:02d}"


def format_progress_entry(meeting_date: date, summary_text: str) -> str:
    """Format a single progress log entry."""
    return f"פגישה בתאריך: {format_progress_date(meeting_date)} - {summary_text.strip()}"


def prepend_progress_entry(existing: str, new_entry: str) -> str:
    """Prepend a new progress entry above existing text."""
    existing_text = existing.strip()
    new_text = new_entry.strip()
    if not new_text:
        return existing_text
    if not existing_text:
        return new_text
    return f"{new_text}\n\n{existing_text}"


async def fetch_contact_progress_text(
    match: ContactMatch,
    settings: CrmSettings,
) -> str:
    """Read the current progress column value for a matched CRM item."""
    columns = CONTACT_PROFILE_COLUMNS.get(match.match_type)
    if not columns:
        raise ValueError(f"No profile column mapping for match type: {match.match_type!r}")

    progress_column_id = columns["progress_column_id"]
    body = await execute_graphql(
        ITEMS_BY_IDS_QUERY,
        {
            "ids": [match.item_id],
            "columnIds": [progress_column_id],
        },
        column_ids=[progress_column_id],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return ""

    for column in items[0].get("column_values") or []:
        if str(column.get("id")) == progress_column_id:
            return column_text(column)
    return ""


async def update_contact_ai_profile(
    match: ContactMatch,
    profile: str,
    latest_date: str,
    settings: CrmSettings,
) -> None:
    """Write AI-generated profile and latest meeting date to the matched CRM item."""
    columns = CONTACT_PROFILE_COLUMNS.get(match.match_type)
    if not columns:
        raise ValueError(f"No profile column mapping for match type: {match.match_type!r}")

    profile_column_id = columns["profile_column_id"]
    date_column_id = columns["latest_date_column_id"]
    board_id = _board_id_for_match(match, settings)

    column_values: dict[str, Any] = {
        profile_column_id: _column_value_for_text(profile_column_id, profile),
        date_column_id: _date_column_value(latest_date),
    }

    await execute_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": board_id,
            "itemId": match.item_id,
            "columnValues": json.dumps(column_values),
        },
        column_ids=list(column_values.keys()),
    )
    logger.info(
        "Updated %s profile columns on board %s item %s",
        match.match_type,
        board_id,
        match.item_id,
    )


async def append_contact_meeting_progress(
    match: ContactMatch,
    meeting_date: date,
    progress_summary: str,
    settings: CrmSettings,
) -> None:
    """Prepend a meeting progress entry to the contact's progress column."""
    columns = CONTACT_PROFILE_COLUMNS.get(match.match_type)
    if not columns:
        raise ValueError(f"No profile column mapping for match type: {match.match_type!r}")

    progress_column_id = columns["progress_column_id"]
    board_id = _board_id_for_match(match, settings)

    existing = await fetch_contact_progress_text(match, settings)
    new_entry = format_progress_entry(meeting_date, progress_summary)
    merged = prepend_progress_entry(existing, new_entry)

    column_values: dict[str, Any] = {
        progress_column_id: _column_value_for_text(progress_column_id, merged),
    }

    await execute_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": board_id,
            "itemId": match.item_id,
            "columnValues": json.dumps(column_values),
        },
        column_ids=list(column_values.keys()),
    )
    logger.info(
        "Appended %s meeting progress on board %s item %s",
        match.match_type,
        board_id,
        match.item_id,
    )
