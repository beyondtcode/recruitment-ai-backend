from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crm_integration.batch import schedule_notetaker_batch
from crm_integration.config import get_crm_settings
from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.schemas import NodeTakerWebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crm"])

BATCH_SECRET_HEADER = "x-batch-secret"


def _batch_secret_from_request(request: Request) -> str | None:
    header = request.headers.get(BATCH_SECRET_HEADER)
    if header:
        return header.strip()
    return None


@router.post("/run-notetaker-batch")
async def run_notetaker_batch_webhook(request: Request) -> JSONResponse:
    """
    Wake-and-schedule endpoint for the nightly Notetaker sync.

    Call at 00:00 (e.g. from an external cron) to wake the server; the batch runs at 00:05
    Asia/Jerusalem unless that time has already passed today, in which case it runs in 5 minutes.
    """
    settings = get_crm_settings()
    if not settings.batch_secret:
        logger.error("run-notetaker-batch called but BATCH_SECRET is not configured")
        return JSONResponse(
            status_code=503,
            content={"status": "error", "detail": "Batch webhook is not configured"},
        )

    provided_secret = _batch_secret_from_request(request)
    if not provided_secret or provided_secret != settings.batch_secret:
        logger.warning("run-notetaker-batch rejected: invalid or missing secret")
        return JSONResponse(
            status_code=401,
            content={"status": "error", "detail": "Unauthorized"},
        )

    run_at, schedule_status, delay_seconds = schedule_notetaker_batch()
    return JSONResponse(
        content={
            "status": schedule_status,
            "runs_at": run_at.isoformat(),
            "delay_seconds": int(delay_seconds),
        }
    )


@router.post("/nodetaker-webhook")
async def nodetaker_webhook(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        logger.warning("NodeTaker webhook: invalid JSON body: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Invalid JSON"},
        )

    try:
        payload = NodeTakerWebhookPayload.model_validate(body)
    except Exception as exc:
        logger.warning("NodeTaker webhook: validation error: %s", exc)
        return JSONResponse(
            status_code=422,
            content={"status": "error", "detail": str(exc)},
        )

    logger.info("NodeTaker webhook received: title=%r", payload.meeting_title)

    try:
        result = await process_nodetaker_webhook(payload)
        return JSONResponse(content=result.model_dump())
    except Exception as exc:
        logger.exception("NodeTaker webhook processing failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(exc)},
        )
