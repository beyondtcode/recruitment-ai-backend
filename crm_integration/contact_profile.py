from __future__ import annotations

import json
import logging
from typing import Any

from crm_integration.config import CrmSettings
from crm_integration.lookup import ContactMatch
from crm_integration.monday_client import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    execute_graphql,
)

logger = logging.getLogger(__name__)

CONTACT_PROFILE_COLUMNS: dict[str, dict[str, str]] = {
    "client": {
        "profile_column_id": "text_mm4mqp9k",
        "latest_date_column_id": "date_mm4d7cs2",
    },
    "lead": {
        "profile_column_id": "text_mm4jajn2",
        "latest_date_column_id": "date_mm4ddb0d",
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
