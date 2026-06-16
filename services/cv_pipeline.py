"""Shared CV processing pipeline for email ingestion and Monday webhooks."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from pydantic import ValidationError

from services.ai_service import analyze_cv_with_claude
from services.monday_service import (
    MAIN_HUB_BOARD_ID,
    get_item_cv_file_url,
    upsert_candidate_item,
)
from utils.file_parser import download_cv_from_url, extract_text_from_file

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
TEMP_CV_DIR = _BACKEND_ROOT / "temp_received_cvs"


async def process_cv_bytes(
    file_bytes: bytes,
    filename: str,
    *,
    board_id: str,
    cv_file_path: str | Path | None = None,
    sync_to_hub: bool = True,
    source_item_id: str | None = None,
) -> None:
    """
    Parse CV bytes with Claude and upsert to the given board (and optionally Main Hub).

    Args:
        file_bytes: Raw CV content.
        filename: Original filename for type detection.
        board_id: Monday board that triggered processing.
        cv_file_path: Local path for file upload on create; defaults to a temp file.
        sync_to_hub: When True and board_id is not Main Hub, also upsert to Main Hub.
        source_item_id: Monday item that triggered processing (webhook); used to avoid
            duplicate rows when email search has not indexed the form submission yet.
    """
    cv_text = extract_text_from_file(file_bytes, filename)
    candidate = await analyze_cv_with_claude(cv_text)

    logger.info(
        "Extracted candidate from %s: %s",
        filename,
        candidate.model_dump_json(ensure_ascii=False),
    )

    upload_path = cv_file_path
    temp_path: Path | None = None
    if upload_path is None:
        TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = TEMP_CV_DIR / filename
        temp_path.write_bytes(file_bytes)
        upload_path = temp_path

    try:
        item_id, created = await upsert_candidate_item(
            candidate,
            cv_file_path=str(upload_path),
            raw_cv_text=cv_text,
            board_id=board_id,
            source_item_id=source_item_id,
        )
        action = "Created" if created else "Updated"
        logger.info("%s Monday item %s on board %s for %s", action, item_id, board_id, filename)

        if sync_to_hub and board_id != MAIN_HUB_BOARD_ID:
            try:
                hub_id, hub_created = await upsert_candidate_item(
                    candidate,
                    cv_file_path=str(upload_path),
                    raw_cv_text=cv_text,
                    board_id=MAIN_HUB_BOARD_ID,
                )
                hub_action = "Created" if hub_created else "Updated"
                logger.info(
                    "%s Main Hub item %s for %s (source board %s)",
                    hub_action,
                    hub_id,
                    filename,
                    board_id,
                )
            except Exception:
                logger.exception(
                    "Main Hub sync failed for %s (source board %s, item already updated on source)",
                    filename,
                    board_id,
                )
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def process_cv_file(file_path: Path, *, board_id: str = MAIN_HUB_BOARD_ID) -> None:
    """Parse a local CV file, extract fields with Claude, and upsert to Monday."""
    file_bytes = file_path.read_bytes()
    await process_cv_bytes(
        file_bytes,
        file_path.name,
        board_id=board_id,
        cv_file_path=file_path.resolve(),
        sync_to_hub=False,
    )
    file_path.unlink(missing_ok=True)
    logger.info("Processed and removed temporary file: %s", file_path.name)


async def process_monday_webhook(item_id: str, board_id: str) -> None:
    """Download CV from a Monday item and run the full extraction + upsert pipeline."""
    logger.info("Processing Monday webhook: item_id=%s board_id=%s", item_id, board_id)

    url, filename = await get_item_cv_file_url(item_id, board_id)
    file_bytes = await download_cv_from_url(url)

    TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
    temp_path = TEMP_CV_DIR / f"{item_id}_{filename}"
    temp_path.write_bytes(file_bytes)

    try:
        await process_cv_bytes(
            file_bytes,
            filename,
            board_id=board_id,
            cv_file_path=temp_path,
            sync_to_hub=True,
            source_item_id=item_id,
        )
    finally:
        temp_path.unlink(missing_ok=True)


def run_webhook_pipeline_sync(item_id: str, board_id: str) -> None:
    """Sync entry point for FastAPI BackgroundTasks."""
    try:
        asyncio.run(process_monday_webhook(item_id, board_id))
    except ValidationError as exc:
        logger.error(
            "Validation failed for Monday item %s on board %s: %s",
            item_id,
            board_id,
            exc,
            exc_info=True,
        )
    except Exception as exc:
        logger.exception(
            "Webhook pipeline failed for item %s on board %s: %s",
            item_id,
            board_id,
            exc,
        )
