"""Exam board (מבחנים) → Main Hub candidate sync + bidirectional Connect links."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from models.candidate import CandidateSchema
from services.ai_service import analyze_cv_with_claude, close_async_client
from services.monday_service import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    FoundItem,
    ITEMS_BY_IDS_QUERY,
    ITEM_CV_FILE_SCOPED_QUERY,
    _fetch_item_brief,
    find_existing_item_by_email,
    get_main_hub_board_id,
    normalize_email,
    normalize_phone,
    post_graphql,
    replace_file_on_item,
    resolve_file_column_id,
    update_candidate_item,
    upsert_candidate_item,
)
from utils.file_parser import download_cv_from_url, extract_text_from_file

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
TEMP_CV_DIR = _BACKEND_ROOT / "temp_received_cvs"

DEFAULT_EXAM_BOARD_ID = "5097679903"
DEFAULT_EXAM_TO_HUB_RELATION_COLUMN_ID = "board_relation_mm3xpjrx"
DEFAULT_HUB_TO_EXAM_RELATION_COLUMN_ID = "board_relation_mm3xds1f"

EXAM_EMAIL_COLUMN_ID = "email_mm3xm226"
EXAM_PHONE_COLUMN_ID = "phone_mm40xg8n"
EXAM_SOURCE_COLUMN_ID = "text_mm3zwgfx"
EXAM_POSITION_COLUMN_ID = "dropdown_mm3xjjdw"
EXAM_TRACK_COLUMN_ID = "color_mm4cdx81"
EXAM_NOTES_COLUMN_ID = "text_mm40g870"

EXAM_FIELD_COLUMN_IDS: tuple[str, ...] = (
    EXAM_EMAIL_COLUMN_ID,
    EXAM_PHONE_COLUMN_ID,
    EXAM_SOURCE_COLUMN_ID,
    EXAM_POSITION_COLUMN_ID,
    EXAM_TRACK_COLUMN_ID,
    EXAM_NOTES_COLUMN_ID,
)


@dataclass(frozen=True)
class ExamItemFields:
    item_id: str
    name: str
    email: str | None
    phone: str | None
    source: str | None
    position: str | None
    track: str | None
    notes: str | None
    linked_hub_item_id: str | None


def get_exam_board_id() -> str:
    return (os.getenv("MONDAY_EXAM_BOARD_ID") or DEFAULT_EXAM_BOARD_ID).strip()


def get_exam_to_hub_relation_column_id() -> str:
    return (
        os.getenv("MONDAY_EXAM_TO_HUB_RELATION_COLUMN_ID")
        or DEFAULT_EXAM_TO_HUB_RELATION_COLUMN_ID
    ).strip()


def get_hub_to_exam_relation_column_id() -> str:
    return (
        os.getenv("MONDAY_HUB_TO_EXAM_RELATION_COLUMN_ID")
        or DEFAULT_HUB_TO_EXAM_RELATION_COLUMN_ID
    ).strip()


def _board_relation_column_value(item_id: str) -> dict[str, list[int]]:
    return {"item_ids": [int(item_id)]}


def _column_text(columns: dict[str, dict[str, Any]], column_id: str) -> str | None:
    column = columns.get(column_id)
    if not column:
        return None
    text = str(column.get("text") or "").strip()
    return text or None


def _email_from_column(column: dict[str, Any] | None) -> str:
    if not column:
        return ""
    text = str(column.get("text") or "").strip()
    value = column.get("value")
    if value:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            if isinstance(parsed, dict):
                email_value = parsed.get("email") or parsed.get("text") or ""
                if email_value:
                    text = str(email_value)
        except (json.JSONDecodeError, TypeError):
            pass
    return normalize_email(text) if text else ""


def _phone_digits_from_column(column: dict[str, Any] | None) -> str:
    if not column:
        return ""
    text = column.get("text") or ""
    value = column.get("value")
    if value:
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
            if isinstance(parsed, dict):
                phone_value = parsed.get("phone") or parsed.get("text") or ""
                text = f"{text} {phone_value}"
        except (json.JSONDecodeError, TypeError):
            pass
    return normalize_phone(str(text))


def _parse_linked_item_id(column: dict[str, Any] | None) -> str | None:
    if not column:
        return None

    linked_item_ids = column.get("linked_item_ids")
    if linked_item_ids:
        first_id = linked_item_ids[0]
        return str(first_id) if first_id is not None else None

    value = column.get("value")
    if not value:
        return None

    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    linked_ids = parsed.get("linkedPulseIds") or parsed.get("item_ids") or []
    if not linked_ids:
        return None

    first = linked_ids[0]
    if isinstance(first, dict):
        first = first.get("linkedPulseId") or first.get("id") or first.get("item_id")
    return str(first) if first is not None else None


def fold_exam_context_into_notes(
    *,
    source: str | None,
    position: str | None,
    track: str | None,
    notes: str | None,
) -> str | None:
    """Build Hub הערות text from exam-only fields that have no Hub column."""
    lines: list[str] = []
    if source:
        lines.append(f"מקור מועמד: {source}")
    if position:
        lines.append(f"משרה: {position}")
    if track:
        lines.append(f"מסלול נבחן: {track}")
    if notes:
        lines.append(f"הערות: {notes}")
    if not lines:
        return None
    return "\n".join(lines)


def candidate_from_exam_fields(fields: ExamItemFields) -> CandidateSchema:
    name = (fields.name or "").strip() or "לא ידוע"
    notes = fold_exam_context_into_notes(
        source=fields.source,
        position=fields.position,
        track=fields.track,
        notes=fields.notes,
    )
    return CandidateSchema(
        name=name,
        email=fields.email,
        phone=fields.phone,
        recruiter_notes=notes,
        extraction_confidence="low",
        confidence_reasoning=(
            "Built from Exam board fields only (no CV). "
            "Identity and notes copied from מבחנים item."
        ),
    )


def _merge_recruiter_notes(
    existing: str | None,
    exam_context: str | None,
) -> str | None:
    existing_text = (existing or "").strip()
    context = (exam_context or "").strip()
    if not context:
        return existing_text or None
    if not existing_text:
        return context
    if context in existing_text:
        return existing_text
    return f"{existing_text}\n\n{context}"


async def fetch_exam_item(item_id: str) -> ExamItemFields:
    """Load identity + context columns from an Exam board item."""
    relation_column_id = get_exam_to_hub_relation_column_id()
    column_ids = list(EXAM_FIELD_COLUMN_IDS) + [relation_column_id]
    body = await post_graphql(
        ITEMS_BY_IDS_QUERY,
        {"ids": [str(item_id)], "columnIds": column_ids},
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        raise ValueError(f"Exam item {item_id} not found")

    item = items[0]
    name = str(item.get("name") or "").strip()
    columns_by_id: dict[str, dict[str, Any]] = {}
    for column in item.get("column_values") or []:
        column_id = column.get("id")
        if column_id:
            columns_by_id[str(column_id)] = column

    email_col = columns_by_id.get(EXAM_EMAIL_COLUMN_ID)
    phone_col = columns_by_id.get(EXAM_PHONE_COLUMN_ID)
    email = _email_from_column(email_col)
    phone = _phone_digits_from_column(phone_col)

    return ExamItemFields(
        item_id=str(item.get("id") or item_id),
        name=name,
        email=email or None,
        phone=phone or None,
        source=_column_text(columns_by_id, EXAM_SOURCE_COLUMN_ID),
        position=_column_text(columns_by_id, EXAM_POSITION_COLUMN_ID),
        track=_column_text(columns_by_id, EXAM_TRACK_COLUMN_ID),
        notes=_column_text(columns_by_id, EXAM_NOTES_COLUMN_ID),
        linked_hub_item_id=_parse_linked_item_id(columns_by_id.get(relation_column_id)),
    )


async def _try_get_exam_cv_file(
    item_id: str,
    board_id: str,
) -> tuple[str, str] | None:
    """Return (url, filename) when a CV is already attached; no multi-second retries."""
    try:
        file_column_id = await resolve_file_column_id(board_id)
    except ValueError:
        logger.info("Exam board %s has no file column; treating as no CV", board_id)
        return None

    body = await post_graphql(
        ITEM_CV_FILE_SCOPED_QUERY,
        {"ids": [str(item_id)], "columnIds": [file_column_id]},
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return None

    for column in items[0].get("column_values") or []:
        files = column.get("files") or []
        if not files:
            continue
        file_entry = files[-1]
        asset = file_entry.get("asset") or {}
        public_url = str(asset.get("public_url") or "").strip()
        name = str(file_entry.get("name") or "").strip()
        extension = str(asset.get("file_extension") or "").strip().lower()
        if not public_url:
            continue
        if not name and extension:
            name = f"cv.{extension}"
        if not name:
            continue
        suffix = Path(name).suffix.lower()
        if suffix not in {".pdf", ".docx"}:
            if extension in {"pdf", "docx"}:
                stem = Path(name).stem or "cv"
                name = f"{stem}.{extension}"
            else:
                continue
        return public_url, name
    return None


async def resolve_existing_hub_item(
    fields: ExamItemFields,
    *,
    hub_board_id: str,
) -> FoundItem | None:
    """Find an existing Main Hub row by exam email before creating a duplicate."""
    email = normalize_email(fields.email or "")
    if email:
        match = await find_existing_item_by_email(email, board_id=hub_board_id)
        if match is not None:
            logger.info(
                "Exam→hub: matched existing hub item %s by email %r",
                match.item_id,
                email,
            )
            return match

    if fields.linked_hub_item_id:
        linked = await _fetch_item_brief(
            fields.linked_hub_item_id,
            board_id=hub_board_id,
        )
        if linked is not None:
            logger.info(
                "Exam→hub: using previously linked hub item %s",
                linked.item_id,
            )
            return linked

    return None


async def _upsert_hub_candidate(
    candidate: CandidateSchema,
    *,
    hub_board_id: str,
    existing_hub: FoundItem | None = None,
    cv_file_path: str | Path | None = None,
    raw_cv_text: str = "",
) -> tuple[str, bool]:
    """Update a resolved hub row or fall back to email/phone upsert."""
    cv_path = str(cv_file_path) if cv_file_path else None

    if existing_hub is not None:
        item_id = await update_candidate_item(
            existing_hub.item_id,
            candidate,
            existing_name=existing_hub.name,
            raw_cv_text=raw_cv_text,
            board_id=hub_board_id,
        )
        if cv_path:
            file_column_id = await resolve_file_column_id(hub_board_id)
            await replace_file_on_item(
                item_id,
                cv_path,
                board_id=hub_board_id,
                column_id=file_column_id,
            )
        logger.info(
            "Exam→hub: updated existing hub item %s (email match)",
            item_id,
        )
        return item_id, False

    return await upsert_candidate_item(
        candidate,
        cv_file_path=cv_path,
        raw_cv_text=raw_cv_text,
        board_id=hub_board_id,
    )


async def link_exam_and_hub(
    *,
    exam_item_id: str,
    hub_item_id: str,
    exam_board_id: str | None = None,
    hub_board_id: str | None = None,
) -> None:
    """Set Connect columns both ways (exam↔hub)."""
    exam_board = exam_board_id or get_exam_board_id()
    hub_board = hub_board_id or get_main_hub_board_id()
    exam_to_hub = get_exam_to_hub_relation_column_id()
    hub_to_exam = get_hub_to_exam_relation_column_id()

    await post_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": exam_board,
            "itemId": str(exam_item_id),
            "columnValues": json.dumps(
                {exam_to_hub: _board_relation_column_value(hub_item_id)}
            ),
        },
        column_ids=[exam_to_hub],
    )
    await post_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": hub_board,
            "itemId": str(hub_item_id),
            "columnValues": json.dumps(
                {hub_to_exam: _board_relation_column_value(exam_item_id)}
            ),
        },
        column_ids=[hub_to_exam],
    )
    logger.info(
        "Linked exam item %s ↔ hub item %s",
        exam_item_id,
        hub_item_id,
    )


async def _upsert_hub_from_cv(
    fields: ExamItemFields,
    *,
    file_bytes: bytes,
    filename: str,
    hub_board_id: str,
    existing_hub: FoundItem | None = None,
) -> tuple[str, bool]:
    cv_text = extract_text_from_file(file_bytes, filename)
    candidate = await analyze_cv_with_claude(cv_text)
    exam_context = fold_exam_context_into_notes(
        source=fields.source,
        position=fields.position,
        track=fields.track,
        notes=fields.notes,
    )
    candidate.recruiter_notes = _merge_recruiter_notes(
        candidate.recruiter_notes,
        exam_context,
    )
    if not (candidate.email or "").strip() and fields.email:
        candidate.email = fields.email
    if not (candidate.phone or "").strip() and fields.phone:
        candidate.phone = fields.phone
    if (not (candidate.name or "").strip() or candidate.name.strip() == "לא ידוע") and fields.name:
        candidate.name = fields.name

    TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
    upload_path = TEMP_CV_DIR / f"{uuid4().hex}_exam_{Path(filename).name}"
    try:
        upload_path.write_bytes(file_bytes)
        return await _upsert_hub_candidate(
            candidate,
            hub_board_id=hub_board_id,
            existing_hub=existing_hub,
            cv_file_path=str(upload_path),
            raw_cv_text=cv_text,
        )
    finally:
        upload_path.unlink(missing_ok=True)


async def process_exam_to_hub(exam_item_id: str, board_id: str) -> None:
    """Upsert Main Hub from an Exam board item and set bidirectional Connect links."""
    exam_board_id = get_exam_board_id()
    if str(board_id) != exam_board_id:
        logger.info(
            "Exam webhook ignored: board %s is not exam board %s",
            board_id,
            exam_board_id,
        )
        return

    logger.info(
        "Processing exam→hub: exam_item_id=%s board_id=%s",
        exam_item_id,
        board_id,
    )

    fields = await fetch_exam_item(exam_item_id)
    hub_board_id = get_main_hub_board_id()
    existing_hub = await resolve_existing_hub_item(fields, hub_board_id=hub_board_id)

    cv_file = await _try_get_exam_cv_file(exam_item_id, board_id)
    if cv_file is not None:
        url, filename = cv_file
        file_bytes = await download_cv_from_url(url)
        hub_item_id, created = await _upsert_hub_from_cv(
            fields,
            file_bytes=file_bytes,
            filename=filename,
            hub_board_id=hub_board_id,
            existing_hub=existing_hub,
        )
        logger.info(
            "%s hub item %s from exam CV on item %s",
            "Created" if created else "Updated",
            hub_item_id,
            exam_item_id,
        )
    else:
        candidate = candidate_from_exam_fields(fields)
        hub_item_id, created = await _upsert_hub_candidate(
            candidate,
            hub_board_id=hub_board_id,
            existing_hub=existing_hub,
        )
        logger.info(
            "%s hub item %s from exam fields on item %s (email=%r phone=%r)",
            "Created" if created else "Updated",
            hub_item_id,
            exam_item_id,
            normalize_email(fields.email or ""),
            normalize_phone(fields.phone or ""),
        )

    await link_exam_and_hub(
        exam_item_id=exam_item_id,
        hub_item_id=hub_item_id,
        exam_board_id=exam_board_id,
        hub_board_id=hub_board_id,
    )


def _run_async_from_sync(coro) -> None:
    try:
        loop = asyncio.get_running_loop()
        on_existing_loop = loop.is_running()
    except RuntimeError:
        loop = None
        on_existing_loop = False

    async def _run() -> None:
        try:
            await coro
        finally:
            if not on_existing_loop:
                await close_async_client()

    if on_existing_loop:
        future = asyncio.run_coroutine_threadsafe(_run(), loop)
        future.result()
    else:
        asyncio.run(_run())


def process_exam_to_hub_sync(item_id: str, board_id: str) -> None:
    """Sync entry point for FastAPI BackgroundTasks."""
    try:
        _run_async_from_sync(process_exam_to_hub(item_id, board_id))
    except ValidationError as exc:
        logger.error(
            "Validation failed for exam item %s on board %s: %s",
            item_id,
            board_id,
            exc,
            exc_info=True,
        )
    except Exception as exc:
        logger.exception(
            "Exam→hub pipeline failed for item %s on board %s: %s",
            item_id,
            board_id,
            exc,
        )
