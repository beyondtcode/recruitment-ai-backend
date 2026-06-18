from __future__ import annotations

import logging
from datetime import datetime, timedelta

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.meeting import meeting_already_exists
from crm_integration.monday_fetcher import ISR_TZ, fetch_notetaker_meetings_since
from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.schemas import NodeTakerWebhookResult

logger = logging.getLogger(__name__)


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
