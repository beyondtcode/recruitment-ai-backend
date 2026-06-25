from __future__ import annotations

import logging

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.contact_profile import update_contact_ai_profile
from crm_integration.lookup import find_contact_by_emails
from crm_integration.meeting import (
    build_meeting_logs_for_profile,
    create_meeting_item,
    gather_past_meeting_context,
)
from crm_integration.schemas import NodeTakerWebhookPayload, NodeTakerWebhookResult
from crm_integration.workdoc import create_meeting_workdoc
from services.ai_service import extract_client_meeting_profile

logger = logging.getLogger(__name__)


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

    match = await find_contact_by_emails(payload.participant_emails, settings=settings)
    if not match:
        logger.warning(
            "Skipping meeting summary: no CRM client/lead match for title=%r date=%s emails=%s",
            payload.meeting_title,
            payload.meeting_date.isoformat(),
            payload.participant_emails,
        )
        return NodeTakerWebhookResult(
            status="skipped",
            match_type="none",
            warnings=["No CRM client/lead match; meeting summary skipped"],
        )

    meeting_item_id = await create_meeting_item(payload, match, settings=settings)

    doc_id: str | None = None
    doc_created = False
    try:
        doc_id, doc_created, doc_warnings = await create_meeting_workdoc(
            meeting_item_id,
            payload,
            settings=settings,
        )
        warnings.extend(doc_warnings)
    except Exception as exc:
        logger.error("Unexpected Workdoc error for item %s: %s", meeting_item_id, exc)
        warnings.append(f"Workdoc step failed: {exc}")

    if payload.meeting_summary.strip():
        try:
            past_context = await gather_past_meeting_context(
                payload.participant_emails,
                before_date=payload.meeting_date,
                settings=settings,
            )
            logs = build_meeting_logs_for_profile(payload, past_context)
            profile, latest_date = await extract_client_meeting_profile(logs)
            await update_contact_ai_profile(match, profile, latest_date, settings)
        except Exception as exc:
            logger.exception("Client profile update failed for match %s", match.item_id)
            warnings.append(f"Client profile update failed: {exc}")
    else:
        warnings.append("Empty meeting summary; profile update skipped")

    return NodeTakerWebhookResult(
        status="success",
        meeting_item_id=meeting_item_id,
        match_type=match.match_type,
        matched_email=match.matched_email,
        doc_id=doc_id,
        doc_created=doc_created,
        warnings=warnings,
    )
