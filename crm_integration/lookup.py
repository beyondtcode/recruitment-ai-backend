from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.monday_client import (
    FIND_ITEMS_LIMIT,
    ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
    ITEMS_PAGE_WITH_COLUMNS_QUERY,
    execute_graphql,
)
from services.monday_service import _fetch_board_columns, normalize_email

logger = logging.getLogger(__name__)

INTERNAL_EMAIL_DOMAIN = "@beyondtcode.com"


def _external_participant_emails(participant_emails: list[str]) -> list[str]:
    """Return participant emails that are not internal @beyondtcode.com addresses."""
    return [
        email
        for email in participant_emails
        if email and not email.strip().lower().endswith(INTERNAL_EMAIL_DOMAIN)
    ]


@dataclass(frozen=True)
class ContactMatch:
    item_id: str
    match_type: Literal["lead"]
    matched_email: str


def _dedupe_emails(emails: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for email in emails:
        normalized = normalize_email(email)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _is_column_not_found_error(exc: BaseException) -> bool:
    return "column not found" in str(exc).casefold()


def _column_text(column: dict) -> str:
    text = str(column.get("text") or "").strip()
    if text:
        return text
    value = column.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _emails_from_additional_text(raw: str) -> set[str]:
    """Normalize emails parsed from the additional-emails text column."""
    result: set[str] = set()
    for part in re.split(r"[,;]", raw):
        normalized = normalize_email(part.strip()) if part.strip() else None
        if normalized:
            result.add(normalized)
    return result


async def _resolve_email_column_ids_for_board(
    board_id: str,
    configured_email_column_id: str,
) -> tuple[str, ...]:
    """Return email column IDs that exist on ``board_id``, configured column first."""
    configured = configured_email_column_id.strip()
    columns = await _fetch_board_columns(board_id)
    if not columns:
        return (configured,) if configured else ()

    board_column_ids = {str(col.get("id") or "") for col in columns}
    resolved: list[str] = []

    if configured and configured in board_column_ids:
        resolved.append(configured)

    for col in columns:
        col_id = str(col.get("id") or "")
        if not col_id or col_id in resolved:
            continue
        col_type = str(col.get("type") or "").casefold()
        if col_type == "email" or col_id.startswith("email_"):
            resolved.append(col_id)

    return tuple(resolved)


async def _query_items_by_email(
    board_id: str,
    email_column_ids: tuple[str, ...],
    email: str,
) -> list[dict[str, str]]:
    for email_column_id in email_column_ids:
        try:
            body = await execute_graphql(
                ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
                {
                    "boardId": board_id,
                    "limit": FIND_ITEMS_LIMIT,
                    "columns": [{"column_id": email_column_id, "column_values": [email]}],
                },
            )
        except Exception as exc:
            if _is_column_not_found_error(exc):
                logger.info(
                    "Email column %s not found on board %s; skipping",
                    email_column_id,
                    board_id,
                )
                continue
            raise
        items = body.get("data", {}).get("items_page_by_column_values", {}).get("items") or []
        if not items:
            continue
        return [
            {"item_id": str(item["id"]), "name": str(item.get("name") or "")}
            for item in items
            if item.get("id") is not None
        ]
    return []


async def _query_items_by_additional_emails(
    board_id: str,
    additional_emails_column_id: str,
    email: str,
) -> list[dict[str, str]]:
    """Find leads whose additional-emails text column contains ``email``.

    Uses ``contains_text`` for candidate retrieval, then verifies the email is an
    exact member of the parsed comma/semicolon-separated set.
    """
    column_id = additional_emails_column_id.strip()
    if not column_id:
        return []

    try:
        body = await execute_graphql(
            ITEMS_PAGE_WITH_COLUMNS_QUERY,
            {
                "boardId": board_id,
                "limit": FIND_ITEMS_LIMIT,
                "columnIds": [column_id],
                "queryParams": {
                    "rules": [
                        {
                            "column_id": column_id,
                            "compare_value": [email],
                            "operator": "contains_text",
                        }
                    ],
                },
            },
            column_ids=[column_id],
        )
    except Exception as exc:
        if _is_column_not_found_error(exc):
            logger.info(
                "Additional emails column %s not found on board %s; skipping",
                column_id,
                board_id,
            )
            return []
        raise

    boards = body.get("data", {}).get("boards") or []
    if not boards:
        return []

    items = (boards[0].get("items_page") or {}).get("items") or []
    matches: list[dict[str, str]] = []
    for item in items:
        item_id = item.get("id")
        if item_id is None:
            continue
        column_values = item.get("column_values") or []
        raw = ""
        for column in column_values:
            if str(column.get("id")) == column_id:
                raw = _column_text(column)
                break
        if email not in _emails_from_additional_text(raw):
            continue
        matches.append(
            {
                "item_id": str(item_id),
                "name": str(item.get("name") or ""),
            }
        )
    return matches


async def _find_on_board(
    emails: list[str],
    *,
    board_id: str,
    configured_email_column_id: str,
    additional_emails_column_id: str,
    board_label: str,
) -> ContactMatch | None:
    email_column_ids = await _resolve_email_column_ids_for_board(
        board_id,
        configured_email_column_id,
    )
    if not email_column_ids and not additional_emails_column_id.strip():
        logger.warning(
            "No email columns resolved for %s board %s; skipping",
            board_label,
            board_id,
        )
        return None

    for email in emails:
        items = []
        match_source = "primary_email"
        if email_column_ids:
            items = await _query_items_by_email(board_id, email_column_ids, email)
        if not items and additional_emails_column_id.strip():
            items = await _query_items_by_additional_emails(
                board_id,
                additional_emails_column_id,
                email,
            )
            match_source = "additional_emails"
        if not items:
            continue
        if len(items) > 1:
            logger.warning(
                "Multiple %s matches for email %s; using first item_id=%s",
                board_label,
                email,
                items[0]["item_id"],
            )
        match = ContactMatch(
            item_id=items[0]["item_id"],
            match_type="lead",
            matched_email=email,
        )
        logger.info(
            "Match found in %s for email %s → item_id %s (source=%s)",
            board_label,
            email,
            match.item_id,
            match_source,
        )
        return match
    return None


async def find_contact_by_emails(
    participant_emails: list[str],
    settings: CrmSettings | None = None,
) -> ContactMatch | None:
    """Find a Lead by participant email on the Leads board.

    Matches the primary email column(s) or any email stored in the additional
    emails text column. Internal @beyondtcode.com addresses are excluded.
    """
    settings = settings or get_crm_settings()
    emails = _dedupe_emails(_external_participant_emails(participant_emails))
    if not emails:
        logger.warning("No valid participant emails provided for contact lookup")
        return None

    lead_match = await _find_on_board(
        emails,
        board_id=settings.monday_crm_leads_board_id,
        configured_email_column_id=settings.monday_crm_leads_email_column_id,
        additional_emails_column_id=settings.monday_crm_leads_additional_emails_column_id,
        board_label="Leads",
    )
    if lead_match:
        return lead_match

    logger.warning("No match found for any participant email: %s", emails)
    return None
