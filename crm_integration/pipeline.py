from __future__ import annotations

import logging

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.contact_profile import (
    append_contact_meeting_progress,
    update_contact_ai_profile,
)
from crm_integration.lookup import find_contact_by_emails
from crm_integration.meeting import (
    BoardKind,
    build_meeting_logs_for_profile,
    create_meeting_item,
    external_participant_emails,
    extract_meeting_summary_intro,
    gather_past_meeting_context,
    internal_participant_emails,
    resolve_beyondcode_client_match,
)
from crm_integration.schemas import NodeTakerWebhookPayload, NodeTakerWebhookResult
from crm_integration.workdoc import create_meeting_workdoc
from services.ai_service import (
    extract_client_meeting_profile,
    extract_meeting_progress_summary,
)

logger = logging.getLogger(__name__)


async def _create_meeting_workdoc_step(
    meeting_item_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
    *,
    board_kind: BoardKind,
) -> tuple[str | None, bool, list[str]]:
    warnings: list[str] = []
    doc_id: str | None = None
    doc_created = False
    try:
        doc_id, doc_created, doc_warnings = await create_meeting_workdoc(
            meeting_item_id,
            payload,
            settings=settings,
            board_kind=board_kind,
        )
        warnings.extend(doc_warnings)
    except Exception as exc:
        logger.error("Unexpected Workdoc error for item %s: %s", meeting_item_id, exc)
        warnings.append(f"Workdoc step failed: {exc}")
    return doc_id, doc_created, warnings


async def process_nodetaker_webhook(
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings | None = None,
) -> NodeTakerWebhookResult:
    settings = settings or get_crm_settings()
    warnings: list[str] = []

    logger.info(
        "NodeTaker webhook processing: title=%r date=%s participants=%d",
        payload.meeting_title,
        payload.meeting_date.isoformat(),
        len(payload.participant_emails),
    )

    external_emails = external_participant_emails(payload.participant_emails)
    internal_emails = internal_participant_emails(payload.participant_emails)

    match = None
    if external_emails:
        match = await find_contact_by_emails(external_emails, settings=settings)

    if match:
        doc_id, doc_created, workdoc_warnings = await _create_meeting_workdoc_step(
            match.item_id,
            payload,
            settings,
            board_kind="customer",
        )
        warnings.extend(workdoc_warnings)

        if payload.meeting_summary.strip():
            try:
                past_context = await gather_past_meeting_context(
                    match.item_id,
                    before_date=payload.meeting_date,
                    settings=settings,
                )
                logs = build_meeting_logs_for_profile(payload, past_context)
                profile, latest_date = await extract_client_meeting_profile(logs)
                await update_contact_ai_profile(match, profile, latest_date, settings)
            except Exception as exc:
                logger.exception("Client profile update failed for match %s", match.item_id)
                warnings.append(f"Client profile update failed: {exc}")

            try:
                progress_summary = await extract_meeting_progress_summary(
                    payload.meeting_title,
                    payload.meeting_date,
                    extract_meeting_summary_intro(payload.meeting_summary),
                    payload.action_items,
                )
                await append_contact_meeting_progress(
                    match,
                    payload.meeting_date,
                    progress_summary,
                    settings,
                )
            except Exception as exc:
                logger.exception("Meeting progress update failed for match %s", match.item_id)
                warnings.append(f"Meeting progress update failed: {exc}")
        else:
            warnings.append("Empty meeting summary; profile update skipped")

        return NodeTakerWebhookResult(
            status="success",
            meeting_item_id=match.item_id,
            match_type=match.match_type,
            matched_email=match.matched_email,
            doc_id=doc_id,
            doc_created=doc_created,
            warnings=warnings,
        )

    if internal_emails:
        match = await resolve_beyondcode_client_match(settings)
        meeting_item_id = await create_meeting_item(
            payload,
            match,
            settings=settings,
            board_kind="company",
        )
        doc_id, doc_created, workdoc_warnings = await _create_meeting_workdoc_step(
            meeting_item_id,
            payload,
            settings,
            board_kind="company",
        )
        warnings.extend(workdoc_warnings)
        return NodeTakerWebhookResult(
            status="success",
            meeting_item_id=meeting_item_id,
            match_type=match.match_type,
            matched_email=match.matched_email,
            doc_id=doc_id,
            doc_created=doc_created,
            warnings=warnings,
        )
    elif external_emails:
        logger.warning(
            "Skipping meeting summary: no CRM lead match for title=%r date=%s emails=%s",
            payload.meeting_title,
            payload.meeting_date.isoformat(),
            external_emails,
        )
        return NodeTakerWebhookResult(
            status="skipped",
            match_type="none",
            warnings=["No CRM lead match; meeting summary skipped"],
        )
    else:
        logger.warning(
            "Skipping meeting summary: no participant emails for title=%r date=%s",
            payload.meeting_title,
            payload.meeting_date.isoformat(),
        )
        return NodeTakerWebhookResult(
            status="skipped",
            match_type="none",
            warnings=["No participant emails; meeting summary skipped"],
        )
