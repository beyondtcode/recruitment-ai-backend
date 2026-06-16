"""FastAPI webhook service for Monday.com CV processing."""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from services.cv_pipeline import run_webhook_pipeline_sync

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Recruitment AI Backend", version="1.0.0")

FILE_COLUMN_WEBHOOK_TYPE = "change_column_value"


def _is_monday_file_column(column_id: object) -> bool:
    return isinstance(column_id, str) and column_id.startswith("file_")


def _extract_ids_from_input_fields(fields: dict[str, Any]) -> tuple[str | None, str | None]:
    item_id = (
        fields.get("itemId")
        or fields.get("pulseId")
        or fields.get("item_id")
        or fields.get("pulse_id")
    )
    board_id = fields.get("boardId") or fields.get("board_id")
    if item_id is None or board_id is None:
        return None, None
    return str(item_id), str(board_id)


def _extract_ids_from_custom_app(body: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    custom_payload = body.get("payload")
    if not isinstance(custom_payload, dict):
        return None, None, None

    input_fields = custom_payload.get("inputFields")
    if not isinstance(input_fields, dict):
        input_fields = custom_payload.get("inboundFieldValues")
    if not isinstance(input_fields, dict):
        return None, None, None

    item_id, board_id = _extract_ids_from_input_fields(input_fields)
    runtime_metadata = body.get("runtimeMetadata")
    trigger_uuid = None
    if isinstance(runtime_metadata, dict):
        trigger_uuid = runtime_metadata.get("triggerUuid")
    return item_id, board_id, trigger_uuid


def _parse_event(event: dict[str, Any]) -> tuple[str | None, str | None, bool]:
    """Return item_id, board_id, and whether the event type was accepted."""
    event_type = event.get("type")
    column_id = event.get("columnId") or event.get("column_id")

    is_form_submission = event_type in ["create_pulse", "create_item"]

    if is_form_submission:
        pass
    elif event_type == FILE_COLUMN_WEBHOOK_TYPE:
        if not _is_monday_file_column(column_id):
            logger.info(
                "Monday webhook ignored: type=%r columnId=%r (expected a file_* column)",
                event_type,
                column_id,
            )
            return None, None, False
    else:
        logger.info(
            "Monday webhook ignored: type=%r (expected create_pulse, create_item, or %r)",
            event_type,
            FILE_COLUMN_WEBHOOK_TYPE,
        )
        return None, None, False

    pulse_id = event.get("pulseId") or event.get("itemId")
    board_id = event.get("boardId")
    if pulse_id is None or board_id is None:
        return None, None, True
    return str(pulse_id), str(board_id), True


def _schedule_cv_pipeline(
    background_tasks: BackgroundTasks,
    item_id: str,
    board_id: str,
    trigger_uuid: object = None,
) -> dict[str, str]:
    logger.info(
        "Monday webhook accepted: item_id=%s board_id=%s trigger=%s",
        item_id,
        board_id,
        trigger_uuid,
    )
    background_tasks.add_task(run_webhook_pipeline_sync, item_id, board_id)
    return {"status": "success"}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/monday-webhook")
async def monday_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning("Monday webhook: invalid JSON body: %s", exc)
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON"})

    if "challenge" in body:
        challenge = body["challenge"]
        logger.info("Monday webhook challenge received")
        return JSONResponse(content={"challenge": challenge})

    custom_payload = body.get("payload")
    if isinstance(custom_payload, dict):
        item_id, board_id, trigger_uuid = _extract_ids_from_custom_app(body)
        if item_id is not None and board_id is not None:
            return JSONResponse(
                content=_schedule_cv_pipeline(background_tasks, item_id, board_id, trigger_uuid)
            )

    event = body.get("event")
    if not isinstance(event, dict):
        logger.info("Monday webhook: no recognized payload or event, ignoring")
        return JSONResponse(content={"status": "ignored"})

    item_id, board_id, event_matched = _parse_event(event)
    if not event_matched:
        return JSONResponse(content={"status": "ignored"})

    if item_id is None or board_id is None:
        logger.warning("Monday webhook: event missing pulseId or boardId: %s", event)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Missing pulseId or boardId"},
        )

    return JSONResponse(
        content=_schedule_cv_pipeline(background_tasks, item_id, board_id, event.get("triggerUuid"))
    )
