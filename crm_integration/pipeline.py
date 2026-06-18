from __future__ import annotations

import logging

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.lookup import find_contact_by_emails
from crm_integration.meeting import create_meeting_item
from crm_integration.schemas import NodeTakerWebhookPayload, NodeTakerWebhookResult
from crm_integration.workdoc import create_meeting_workdoc

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

    if not payload.participant_emails:
        warnings.append("No participant emails provided; board relation will be skipped")

    match = await find_contact_by_emails(payload.participant_emails, settings=settings)

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

    return NodeTakerWebhookResult(
        status="success",
        meeting_item_id=meeting_item_id,
        match_type=match.match_type if match else "none",
        matched_email=match.matched_email if match else None,
        doc_id=doc_id,
        doc_created=doc_created,
        warnings=warnings,
    )
