"""FastAPI webhook service for Monday.com CV processing."""

from __future__ import annotations

import logging
from typing import Any

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from services.cv_pipeline import run_webhook_pipeline_sync
from services.monday_service import FILE_COLUMN_ID

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Recruitment AI Backend", version="1.0.0")

CREATE_ITEM_WEBHOOK_TYPE = "create_item"
FILE_COLUMN_WEBHOOK_TYPE = "change_column_value"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/monday-webhook")
async def monday_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        payload: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning("Monday webhook: invalid JSON body: %s", exc)
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON"})

    if "challenge" in payload:
        challenge = payload["challenge"]
        logger.info("Monday webhook challenge received")
        return JSONResponse(content={"challenge": challenge})

    event = payload.get("event")
    if not isinstance(event, dict):
        logger.info("Monday webhook: no event in payload, ignoring")
        return JSONResponse(content={"status": "ignored"})

    event_type = event.get("type")
    column_id = event.get("columnId") or event.get("column_id")

    if event_type == CREATE_ITEM_WEBHOOK_TYPE:
        pass
    elif event_type == FILE_COLUMN_WEBHOOK_TYPE:
        if str(column_id) != FILE_COLUMN_ID:
            logger.info(
                "Monday webhook ignored: type=%r columnId=%r (expected columnId=%r)",
                event_type,
                column_id,
                FILE_COLUMN_ID,
            )
            return JSONResponse(content={"status": "ignored"})
    else:
        logger.info(
            "Monday webhook ignored: type=%r (expected %r or %r)",
            event_type,
            CREATE_ITEM_WEBHOOK_TYPE,
            FILE_COLUMN_WEBHOOK_TYPE,
        )
        return JSONResponse(content={"status": "ignored"})

    pulse_id = event.get("pulseId") or event.get("itemId")
    board_id = event.get("boardId")

    if pulse_id is None or board_id is None:
        logger.warning("Monday webhook: file column event missing pulseId or boardId: %s", event)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Missing pulseId or boardId"},
        )

    item_id = str(pulse_id)
    board_id_str = str(board_id)

    logger.info(
        "Monday webhook accepted: item_id=%s board_id=%s trigger=%s",
        item_id,
        board_id_str,
        event.get("triggerUuid"),
    )

    background_tasks.add_task(run_webhook_pipeline_sync, item_id, board_id_str)
    return JSONResponse(content={"status": "success"})
