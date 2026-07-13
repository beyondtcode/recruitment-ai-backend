from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import EmailStr, TypeAdapter, ValidationError

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.lookup import find_contact_by_emails
from crm_integration.meeting import (
    _column_by_id,
    column_text,
    create_meeting_subitem,
    date_column_value,
    find_existing_meeting_subitem_id,
    parse_comma_separated_emails,
)
from crm_integration.monday_client import (
    delete_monday_item,
    fetch_item_doc_id,
    fetch_items_by_ids,
)
from crm_integration.pipeline import run_lead_meeting_enrichment
from crm_integration.schemas import NodeTakerWebhookPayload, RematchWebhookResult
from crm_integration.workdoc import copy_workdoc_blocks, create_meeting_workdoc
from services.monday_service import normalize_email

logger = logging.getLogger(__name__)

EMAIL_VALIDATOR = TypeAdapter(EmailStr)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _valid_email(email: str) -> str | None:
    try:
        return str(EMAIL_VALIDATOR.validate_python(email))
    except ValidationError:
        return None


def _extract_participant_emails(raw: str) -> list[str]:
    """Extract valid emails from participant text (supports 'Name (email)' format)."""
    emails: list[str] = []
    seen: set[str] = set()

    for part in re.split(r"[,;]", raw):
        part = part.strip()
        if not part:
            continue
        candidates = EMAIL_PATTERN.findall(part) or ([part] if "@" in part else [])
        for candidate in candidates:
            normalized = normalize_email(candidate)
            validated = _valid_email(normalized) if normalized else None
            if validated and validated not in seen:
                seen.add(validated)
                emails.append(validated)
    return emails


def _general_meeting_column_ids(settings: CrmSettings) -> list[str]:
    return [
        settings.monday_crm_meeting_date_column_id,
        settings.monday_crm_meeting_summary_column_id,
        settings.monday_crm_meeting_action_items_column_id,
        settings.monday_crm_meeting_external_participants_column_id,
        settings.monday_crm_meeting_doc_column_id,
        settings.meeting_notes_reminder_date_column_id,
        settings.meeting_notes_reminder_info_column_id,
    ]


def _reminder_from_item(
    item: dict[str, Any],
    settings: CrmSettings,
) -> dict[str, str] | None:
    reminder_date_col = _column_by_id(item, settings.meeting_notes_reminder_date_column_id)
    reminder_info_col = _column_by_id(item, settings.meeting_notes_reminder_info_column_id)

    reminder_date = date_column_value(reminder_date_col or {})
    reminder_info = column_text(reminder_info_col or {})

    if not reminder_date and not reminder_info:
        return None

    reminder: dict[str, str] = {}
    if reminder_date:
        reminder["date"] = reminder_date.isoformat()
    if reminder_info:
        reminder["info"] = reminder_info
    return reminder


def payload_from_general_meeting_item(
    item: dict[str, Any],
    settings: CrmSettings,
) -> tuple[NodeTakerWebhookPayload | None, str | None]:
    """Build a NodeTaker payload from a General Meetings board item."""
    title = str(item.get("name") or "").strip()
    if not title:
        return None, "Missing item name"

    date_col = _column_by_id(item, settings.monday_crm_meeting_date_column_id)
    meeting_date = date_column_value(date_col or {})
    if meeting_date is None:
        return None, "Missing or invalid meeting date"

    summary_col = _column_by_id(item, settings.monday_crm_meeting_summary_column_id)
    action_items_col = _column_by_id(item, settings.monday_crm_meeting_action_items_column_id)
    external_col = _column_by_id(
        item,
        settings.monday_crm_meeting_external_participants_column_id,
    )

    summary = column_text(summary_col or {})
    action_items = column_text(action_items_col or {})
    external_raw = column_text(external_col or {})
    external_emails = _extract_participant_emails(external_raw)
    if not external_emails:
        external_emails = [
            validated
            for email in parse_comma_separated_emails(external_raw)
            if (validated := _valid_email(normalize_email(email) or ""))
        ]

    try:
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "participant_emails": external_emails,
                "meeting_summary": summary,
                "action_items": action_items,
            }
        )
    except ValidationError as exc:
        return None, str(exc)

    return payload, None


async def _attach_workdoc(
    source_item_id: str,
    subitem_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
) -> tuple[str | None, bool, list[str]]:
    warnings: list[str] = []
    source_doc_id = await fetch_item_doc_id(
        source_item_id,
        settings.monday_crm_meeting_doc_column_id,
    )
    if source_doc_id:
        doc_id, doc_created, copy_warnings = await copy_workdoc_blocks(
            source_doc_id,
            subitem_id,
            settings,
            board_kind="subitem",
        )
        warnings.extend(copy_warnings)
        if doc_created and doc_id:
            return doc_id, True, warnings
        for warning in copy_warnings:
            logger.warning("Workdoc copy failed for %s: %s", source_item_id, warning)

    doc_id, doc_created, rebuild_warnings = await create_meeting_workdoc(
        subitem_id,
        payload,
        settings,
        board_kind="subitem",
    )
    warnings.extend(rebuild_warnings)
    return doc_id, doc_created, warnings


async def process_general_meeting_rematch(
    item_id: str,
    settings: CrmSettings | None = None,
) -> RematchWebhookResult:
    """Rematch a General Meetings board item onto Path A (lead subitem)."""
    settings = settings or get_crm_settings()
    warnings: list[str] = []
    source_item_id = str(item_id).strip()
    if not source_item_id:
        return RematchWebhookResult(
            status="error",
            warnings=["Missing item_id"],
        )

    logger.info("General meeting rematch started: source_item_id=%s", source_item_id)

    column_ids = _general_meeting_column_ids(settings)
    items = await fetch_items_by_ids([source_item_id], column_ids)
    if not items:
        return RematchWebhookResult(
            status="error",
            source_item_id=source_item_id,
            warnings=[f"General meeting item {source_item_id} not found"],
        )

    item = items[0]
    payload, payload_error = payload_from_general_meeting_item(item, settings)
    if payload is None:
        return RematchWebhookResult(
            status="error",
            source_item_id=source_item_id,
            warnings=[payload_error or "Failed to build meeting payload"],
        )

    match = await find_contact_by_emails(
        [str(email) for email in payload.participant_emails],
        settings=settings,
    )
    if not match:
        logger.info(
            "General meeting rematch skipped: no lead match for item %s emails=%s",
            source_item_id,
            payload.participant_emails,
        )
        return RematchWebhookResult(
            status="skipped",
            source_item_id=source_item_id,
            match_type="none",
            warnings=["No CRM lead match; general meeting rematch skipped"],
        )

    existing_subitem_id = await find_existing_meeting_subitem_id(
        payload,
        match.item_id,
        settings=settings,
    )
    if existing_subitem_id:
        meeting_item_id = existing_subitem_id
        warnings.append(
            f"Meeting subitem already exists (id={existing_subitem_id}); skipped creation"
        )
        logger.info(
            "Rematch using existing subitem %s under lead %s",
            existing_subitem_id,
            match.item_id,
        )
    else:
        reminder = _reminder_from_item(item, settings)
        meeting_item_id = await create_meeting_subitem(
            payload,
            match,
            settings=settings,
            reminder=reminder,
            fetch_mirly_reminder=reminder is None,
        )

    doc_id, doc_created, workdoc_warnings = await _attach_workdoc(
        source_item_id,
        meeting_item_id,
        payload,
        settings,
    )
    warnings.extend(workdoc_warnings)
    warnings.extend(await run_lead_meeting_enrichment(payload, match, settings))

    source_deleted = False
    try:
        await delete_monday_item(source_item_id)
        source_deleted = True
        logger.info(
            "Deleted General Meetings item %s after rematch to subitem %s",
            source_item_id,
            meeting_item_id,
        )
    except Exception as exc:
        logger.exception("Failed to delete General Meetings item %s", source_item_id)
        warnings.append(f"Source item delete failed: {exc}")

    return RematchWebhookResult(
        status="success",
        source_item_id=source_item_id,
        meeting_item_id=meeting_item_id,
        match_type=match.match_type,
        matched_email=match.matched_email,
        doc_id=doc_id,
        doc_created=doc_created,
        source_deleted=source_deleted,
        warnings=warnings,
    )
