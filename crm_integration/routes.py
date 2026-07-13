from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crm_integration.config import get_crm_settings
from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.rematch import process_general_meeting_rematch
from crm_integration.schemas import NodeTakerWebhookPayload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["crm"])


def _extract_ids_from_input_fields(fields: dict[str, Any]) -> tuple[str | None, str | None]:
    item_id = (
        fields.get("itemId")
        or fields.get("pulseId")
        or fields.get("item_id")
        or fields.get("pulse_id")
    )
    board_id = fields.get("boardId") or fields.get("board_id")
    item_id_str = str(item_id) if item_id is not None else None
    board_id_str = str(board_id) if board_id is not None else None
    return item_id_str, board_id_str


def _extract_ids_from_custom_app(body: dict[str, Any]) -> tuple[str | None, str | None]:
    custom_payload = body.get("payload")
    if not isinstance(custom_payload, dict):
        return None, None

    input_fields = custom_payload.get("inputFields")
    if not isinstance(input_fields, dict):
        input_fields = custom_payload.get("inboundFieldValues")
    if not isinstance(input_fields, dict):
        return None, None

    return _extract_ids_from_input_fields(input_fields)


def _parse_rematch_item_id(body: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (item_id, board_id) from Monday event, custom app, or simple JSON."""
    top_item = body.get("item_id") or body.get("pulseId") or body.get("itemId")
    top_board = body.get("board_id") or body.get("boardId")
    if top_item is not None:
        return str(top_item), str(top_board) if top_board is not None else None

    event = body.get("event")
    if isinstance(event, dict):
        pulse_id = event.get("pulseId") or event.get("itemId")
        board_id = event.get("boardId") or event.get("board_id")
        if pulse_id is not None:
            return (
                str(pulse_id),
                str(board_id) if board_id is not None else None,
            )

    return _extract_ids_from_custom_app(body)


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


@router.post("/general-meeting-rematch-webhook")
async def general_meeting_rematch_webhook(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:
        logger.warning("General meeting rematch webhook: invalid JSON body: %s", exc)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Invalid JSON"},
        )

    if not isinstance(body, dict):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Expected a JSON object"},
        )

    challenge = body.get("challenge")
    if challenge is not None:
        return JSONResponse(content={"challenge": challenge})

    item_id, board_id = _parse_rematch_item_id(body)
    if not item_id:
        return JSONResponse(
            status_code=422,
            content={"status": "error", "detail": "Missing item_id / pulseId"},
        )

    settings = get_crm_settings()
    expected_board = settings.monday_crm_company_meetings_board_id.strip()
    if board_id and expected_board and board_id != expected_board:
        logger.info(
            "General meeting rematch ignored: board_id=%s expected=%s",
            board_id,
            expected_board,
        )
        return JSONResponse(
            content={
                "status": "ignored",
                "detail": f"board_id {board_id} is not the General Meetings board",
            }
        )

    logger.info("General meeting rematch webhook received: item_id=%s", item_id)

    try:
        result = await process_general_meeting_rematch(item_id, settings=settings)
        return JSONResponse(content=result.model_dump())
    except Exception as exc:
        logger.exception("General meeting rematch failed for item %s: %s", item_id, exc)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": str(exc)},
        )
