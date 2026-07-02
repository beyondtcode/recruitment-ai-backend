from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.schemas import NodeTakerWebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crm"])


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
