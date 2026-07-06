"""Daily email CV batch: IMAP fetch, validation, dedup, and Main Hub upsert."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from pydantic import ValidationError

from crm_integration.monday_fetcher import ISR_TZ
from services.cv_pipeline import CvPipelineSkipped, process_cv_bytes
from services.email_service import TEMP_CV_DIR, CvEmailAttachment, fetch_cv_attachments
from services.monday_service import get_main_hub_board_id
from services.position_routing import (
    PositionMatch,
    RoutingContext,
    load_routing_context,
    match_email_to_position,
    resolve_job_requirements_for_match,
)
from utils.file_parser import extract_text_from_file, is_plausible_cv_text

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_STATE_FILE = TEMP_CV_DIR / ".processed_attachments.json"
STATE_RETENTION_DAYS = 7
MIN_CV_TEXT_CHARS = 200


def _email_credentials_configured() -> bool:
    load_dotenv(_BACKEND_ROOT / ".env")
    return bool(
        os.getenv("EMAIL_HOST")
        and os.getenv("EMAIL_USER")
        and os.getenv("EMAIL_PASSWORD")
    )


def _dedup_key(attachment: CvEmailAttachment) -> str:
    return f"{attachment.message_id}:{attachment.sha256}"


def _load_processed_state() -> dict[str, str]:
    if not PROCESSED_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(PROCESSED_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read processed attachment state; starting fresh")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _prune_processed_state(state: dict[str, str], *, now: datetime) -> dict[str, str]:
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    pruned: dict[str, str] = {}
    for key, raw_ts in state.items():
        try:
            processed_at = datetime.fromisoformat(raw_ts)
        except ValueError:
            continue
        if processed_at.tzinfo is None:
            processed_at = processed_at.replace(tzinfo=ISR_TZ)
        if processed_at >= cutoff:
            pruned[key] = raw_ts
    return pruned


def _save_processed_state(state: dict[str, str]) -> None:
    TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _record_processed(state: dict[str, str], key: str, *, now: datetime) -> None:
    state[key] = now.isoformat()


def _validate_cv_text(file_bytes: bytes, filename: str) -> str | None:
    """Return a skip reason if the attachment text fails pre-Claude validation."""
    try:
        text = extract_text_from_file(file_bytes, filename)
    except ValueError:
        return "unreadable_file"

    if len(text.strip()) < MIN_CV_TEXT_CHARS:
        return "text_too_short"

    if not is_plausible_cv_text(text):
        return "not_cv_content"

    return None


def _routing_detail(
    attachment: CvEmailAttachment,
    match: PositionMatch | None,
    *,
    routed: bool,
) -> dict[str, object]:
    return {
        "filename": attachment.filename,
        "subject": attachment.subject,
        "matched_position": match.position.item_name if match else None,
        "board_id": match.board_id if match else None,
        "routed_to_job_board": routed,
    }


async def _process_attachment(
    attachment: CvEmailAttachment,
    *,
    processed_state: dict[str, str],
    now: datetime,
    routing_context: RoutingContext | None,
) -> tuple[str, str | None, dict[str, object] | None]:
    """
    Process one attachment.

    Returns (outcome, skip_reason, routing_detail) where outcome is one of:
    created, updated, skipped, error.
    """
    file_path = Path(attachment.path)
    dedup_key = _dedup_key(attachment)
    if dedup_key in processed_state:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "already_processed", None

    try:
        file_bytes = file_path.read_bytes()
        skip_reason = _validate_cv_text(file_bytes, attachment.filename)
        if skip_reason:
            file_path.unlink(missing_ok=True)
            return "skipped", skip_reason, None

        match: PositionMatch | None = None
        if routing_context is not None:
            match = match_email_to_position(
                attachment.subject,
                attachment.body_snippet,
                routing_context.active_positions,
                board_name_to_id=routing_context.board_name_to_id,
            )

        if match and match.board_id:
            job_requirements = await resolve_job_requirements_for_match(match)
            result = await process_cv_bytes(
                file_bytes,
                attachment.filename,
                board_id=match.board_id,
                sync_to_hub=True,
                job_requirements=job_requirements,
                reject_low_confidence_no_identity=True,
            )
            route_detail = _routing_detail(attachment, match, routed=True)
        else:
            result = await process_cv_bytes(
                file_bytes,
                attachment.filename,
                board_id=get_main_hub_board_id(),
                sync_to_hub=False,
                reject_low_confidence_no_identity=True,
            )
            route_detail = _routing_detail(attachment, match, routed=False)

        file_path.unlink(missing_ok=True)
        _record_processed(processed_state, dedup_key, now=now)
        return ("created" if result.created else "updated"), None, route_detail
    except CvPipelineSkipped as exc:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", exc.reason, None
    except ValidationError:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "cv_validation_failed", None
    except ValueError:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "cv_validation_failed", None
    except Exception:
        logger.exception("Failed to process email attachment %s", attachment.filename)
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "error", None, None


async def process_email_cv_batch(*, lookback_days: int = 1) -> dict[str, object]:
    """
    Fetch CV attachments from email, validate, deduplicate, and upsert to Monday.

    When an email subject matches an actively recruiting position on the position-
    requests board, the CV is upserted to that job board and synced to Main Hub.
    Otherwise the CV is upserted to Main Hub only.

    Returns a summary dict with counts and optional per-file skip details.
    """
    empty_summary = {
        "status": "skipped",
        "reason": "email_not_configured",
        "attachment_count": 0,
        "skipped_count": 0,
        "created_count": 0,
        "updated_count": 0,
        "error_count": 0,
        "routed_to_job_board_count": 0,
        "main_hub_only_count": 0,
        "skipped": [],
        "routing": [],
    }

    if not _email_credentials_configured():
        logger.warning("Email CV batch skipped: EMAIL_HOST/USER/PASSWORD not configured")
        return empty_summary

    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    processed_state = _prune_processed_state(_load_processed_state(), now=now)

    routing_context: RoutingContext | None = None
    try:
        routing_context = await load_routing_context()
    except Exception:
        logger.exception(
            "Failed to load position routing context; falling back to Main Hub only"
        )

    try:
        attachments = await asyncio.to_thread(
            fetch_cv_attachments,
            lookback_days=lookback_days,
        )
    except Exception:
        logger.exception("Email CV batch failed while fetching attachments")
        return {
            "status": "error",
            "attachment_count": 0,
            "skipped_count": 0,
            "created_count": 0,
            "updated_count": 0,
            "error_count": 1,
            "routed_to_job_board_count": 0,
            "main_hub_only_count": 0,
            "skipped": [],
            "routing": [],
        }

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0
    routed_to_job_board_count = 0
    main_hub_only_count = 0
    skipped_details: list[dict[str, str]] = []
    routing_details: list[dict[str, object]] = []

    for attachment in attachments:
        outcome, skip_reason, route_detail = await _process_attachment(
            attachment,
            processed_state=processed_state,
            now=now,
            routing_context=routing_context,
        )
        if outcome == "created":
            created_count += 1
        elif outcome == "updated":
            updated_count += 1
        elif outcome == "skipped":
            skipped_count += 1
            skipped_details.append(
                {
                    "filename": attachment.filename,
                    "reason": skip_reason or "skipped",
                }
            )
        elif outcome == "error":
            error_count += 1

        if route_detail is not None:
            routing_details.append(route_detail)
            if route_detail.get("routed_to_job_board"):
                routed_to_job_board_count += 1
            else:
                main_hub_only_count += 1

    _save_processed_state(processed_state)

    return {
        "status": "ok",
        "attachment_count": len(attachments),
        "skipped_count": skipped_count,
        "created_count": created_count,
        "updated_count": updated_count,
        "error_count": error_count,
        "routed_to_job_board_count": routed_to_job_board_count,
        "main_hub_only_count": main_hub_only_count,
        "skipped": skipped_details,
        "routing": routing_details,
    }
