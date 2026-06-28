"""FastAPI webhook service for Monday.com CV processing."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from crm_integration.batch import process_morning_briefs
from crm_integration.monday_fetcher import ISR_TZ, fetch_meeting_by_participants
from crm_integration.pipeline import process_nodetaker_webhook
from crm_integration.routes import router as crm_router
from services.cv_pipeline import run_webhook_pipeline_sync
from services.email_batch import process_email_cv_batch

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _run_morning_briefs() -> None:
    logger.info("Morning briefing batch started")
    try:
        summary = await process_morning_briefs()
        logger.info(
            "Morning briefing batch finished: processed=%d skipped=%d errors=%d",
            summary["processed_count"],
            summary["skipped_count"],
            summary["error_count"],
        )
    except Exception:
        logger.exception("Morning briefing batch failed")


async def _run_email_cv_batch() -> None:
    logger.info("Daily email CV batch started")
    try:
        summary = await process_email_cv_batch()
        logger.info(
            "Daily email CV batch finished: attachments=%d created=%d updated=%d "
            "skipped=%d errors=%d",
            summary.get("attachment_count", 0),
            summary.get("created_count", 0),
            summary.get("updated_count", 0),
            summary.get("skipped_count", 0),
            summary.get("error_count", 0),
        )
    except Exception:
        logger.exception("Daily email CV batch failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler(timezone=ISR_TZ)
    scheduler.add_job(
        _run_morning_briefs,
        trigger="cron",
        hour=7,
        minute=0,
        id="morning_briefing_batch",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_email_cv_batch,
        trigger="cron",
        hour=8,
        minute=0,
        id="daily_email_cv_batch",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "APScheduler started: morning briefings at 07:00, email CV batch at 08:00 "
        "Asia/Jerusalem (notetaker batch is triggered via POST /run-notetaker-batch webhook)"
    )
    yield
    scheduler.shutdown(wait=False)
    logger.info("APScheduler shut down")


app = FastAPI(
    title="Recruitment AI Backend",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(crm_router)

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


@app.get("/test-fetch-sarah")
async def test_fetch_sarah() -> JSONResponse:
    """Manual test: pull yesterday's Sarah meeting from Notetaker and run the CRM pipeline."""
    yesterday = date.today() - timedelta(days=1)
    email1 = "dev@beyondtcode.com"
    email2 = "saramauda06@gmail.com"

    logger.info(
        "test-fetch-sarah: searching for meeting between %s and %s on %s",
        email1,
        email2,
        yesterday.isoformat(),
    )

    payload = await fetch_meeting_by_participants(email1, email2, yesterday)
    if payload is None:
        return JSONResponse(
            content={
                "status": "not_found",
                "search_date": yesterday.isoformat(),
                "participants": [email1, email2],
            }
        )

    logger.info(
        "test-fetch-sarah: found meeting title=%r date=%s summary_len=%d action_items_len=%d",
        payload.meeting_title,
        payload.meeting_date.isoformat(),
        len(payload.meeting_summary),
        len(payload.action_items),
    )

    try:
        result = await process_nodetaker_webhook(payload)
        return JSONResponse(
            content={
                "status": "processed",
                "meeting": payload.model_dump(mode="json"),
                "pipeline": result.model_dump(mode="json"),
            }
        )
    except Exception as exc:
        logger.exception("test-fetch-sarah: pipeline failed: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "status": "error",
                "detail": str(exc),
                "meeting": payload.model_dump(mode="json"),
            },
        )


@app.post("/monday-webhook")
async def monday_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    try:
        body: dict[str, Any] = await request.json()
    except Exception as exc:
        logger.warning("Monday webhook: invalid JSON body: %s", exc)
        return JSONResponse(status_code=400, content={"status": "error", "detail": "Invalid JSON"})

    logger.info(f"Raw body received: {body}")

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
