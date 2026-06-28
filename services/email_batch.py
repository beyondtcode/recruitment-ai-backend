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
from services.cv_pipeline import CvPipelineSkipped, process_cv_file
from services.email_service import TEMP_CV_DIR, CvEmailAttachment, fetch_cv_attachments
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


async def _process_attachment(
    attachment: CvEmailAttachment,
    *,
    processed_state: dict[str, str],
    now: datetime,
) -> tuple[str, str | None]:
    """
    Process one attachment.

    Returns (outcome, skip_reason) where outcome is one of:
    created, updated, skipped, error.
    """
    file_path = Path(attachment.path)
    dedup_key = _dedup_key(attachment)
    if dedup_key in processed_state:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "already_processed"

    try:
        file_bytes = file_path.read_bytes()
        skip_reason = _validate_cv_text(file_bytes, attachment.filename)
        if skip_reason:
            file_path.unlink(missing_ok=True)
            return "skipped", skip_reason

        result = await process_cv_file(
            file_path,
            reject_low_confidence_no_identity=True,
        )
        _record_processed(processed_state, dedup_key, now=now)
        return ("created" if result.created else "updated"), None
    except CvPipelineSkipped as exc:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", exc.reason
    except ValidationError:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "cv_validation_failed"
    except ValueError:
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "skipped", "cv_validation_failed"
    except Exception:
        logger.exception("Failed to process email attachment %s", attachment.filename)
        if file_path.exists():
            file_path.unlink(missing_ok=True)
        return "error", None


async def process_email_cv_batch(*, lookback_days: int = 1) -> dict[str, object]:
    """
    Fetch CV attachments from email, validate, deduplicate, and upsert to Main Hub.

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
        "skipped": [],
    }

    if not _email_credentials_configured():
        logger.warning("Email CV batch skipped: EMAIL_HOST/USER/PASSWORD not configured")
        return empty_summary

    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    processed_state = _prune_processed_state(_load_processed_state(), now=now)

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
            "skipped": [],
        }

    created_count = 0
    updated_count = 0
    skipped_count = 0
    error_count = 0
    skipped_details: list[dict[str, str]] = []

    for attachment in attachments:
        outcome, skip_reason = await _process_attachment(
            attachment,
            processed_state=processed_state,
            now=now,
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

    _save_processed_state(processed_state)

    return {
        "status": "ok",
        "attachment_count": len(attachments),
        "skipped_count": skipped_count,
        "created_count": created_count,
        "updated_count": updated_count,
        "error_count": error_count,
        "skipped": skipped_details,
    }
