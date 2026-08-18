"""Supplier invoice batch: fetch emails from Monday and trigger n8n webhooks concurrently."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from crm_integration.monday_client import fetch_all_board_items
from services.monday_service import _email_from_column, normalize_email

logger = logging.getLogger(__name__)

DEFAULT_EMAIL_COLUMN_ID = "email_mm5fbcxc"
DEFAULT_BATCH_DELAY_SECONDS = 0.0
DEFAULT_BATCH_CONCURRENCY = 10
WEBHOOK_TIMEOUT_SECONDS = 30.0


def _webhook_url() -> str:
    return (os.getenv("N8N_INVOICE_WEBHOOK_URL") or "").strip()


def _batch_delay_seconds() -> float:
    """Optional stagger between webhook starts (seconds). Default 0 = no delay."""
    raw = os.getenv("INVOICE_BATCH_DELAY_SECONDS", str(DEFAULT_BATCH_DELAY_SECONDS))
    try:
        delay = float(raw)
    except ValueError:
        logger.warning(
            "Invalid INVOICE_BATCH_DELAY_SECONDS=%r; using default %.1f",
            raw,
            DEFAULT_BATCH_DELAY_SECONDS,
        )
        return DEFAULT_BATCH_DELAY_SECONDS
    return max(0.0, delay)


def _batch_concurrency() -> int:
    """Max concurrent webhook POSTs. Values <= 0 mean unlimited."""
    raw = os.getenv("INVOICE_BATCH_CONCURRENCY", str(DEFAULT_BATCH_CONCURRENCY))
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "Invalid INVOICE_BATCH_CONCURRENCY=%r; using default %d",
            raw,
            DEFAULT_BATCH_CONCURRENCY,
        )
        return DEFAULT_BATCH_CONCURRENCY
    return value


def _suppliers_board_id() -> str:
    return (os.getenv("MONDAY_SUPPLIERS_BOARD_ID") or "").strip()


def _supplier_email_column_id() -> str:
    return (
        os.getenv("MONDAY_SUPPLIER_EMAIL_COLUMN_ID") or DEFAULT_EMAIL_COLUMN_ID
    ).strip() or DEFAULT_EMAIL_COLUMN_ID


def _column_dict(item: dict[str, Any], column_id: str) -> dict[str, Any]:
    for column in item.get("column_values") or []:
        if isinstance(column, dict) and column.get("id") == column_id:
            return column
    return {}


def _empty_summary(*, reason: str, status: str = "skipped") -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "emails_found": 0,
        "unique": 0,
        "total": 0,
        "triggered": 0,
        "failed": 0,
        "skipped": 0,
        "errors": [],
    }


async def fetch_supplier_emails_from_monday() -> tuple[list[str], int]:
    """Fetch supplier emails from the configured Monday board.

    Returns ``(unique_emails_sorted, total_emails_found)`` where
    ``total_emails_found`` counts every non-empty email before deduplication.
    """
    board_id = _suppliers_board_id()
    if not board_id:
        raise ValueError(
            "MONDAY_SUPPLIERS_BOARD_ID is required when supplier emails are not provided"
        )

    email_column_id = _supplier_email_column_id()
    logger.info(
        "Fetching supplier emails from Monday board=%s column=%s",
        board_id,
        email_column_id,
    )
    items = await fetch_all_board_items(board_id, [email_column_id])

    emails: set[str] = set()
    total_found = 0
    for item in items:
        email = _email_from_column(_column_dict(item, email_column_id))
        if email:
            total_found += 1
            emails.add(email)

    result = sorted(emails)
    logger.info(
        "Found %d supplier emails (%d unique) from %d board items",
        total_found,
        len(result),
        len(items),
    )
    return result, total_found


async def _trigger_one_webhook(
    client: httpx.AsyncClient,
    webhook_url: str,
    email: str,
    *,
    index: int,
    total: int,
    semaphore: asyncio.Semaphore,
    start_delay: float,
) -> tuple[str, bool, str | None]:
    """POST a single supplier email to n8n. Never raises — returns success flag."""
    if start_delay > 0:
        await asyncio.sleep(start_delay)

    async with semaphore:
        try:
            response = await client.post(
                webhook_url,
                json={"supplier_email": email},
            )
            response.raise_for_status()
            logger.info(
                "n8n invoice webhook ok (%d/%d): email=%s status=%s",
                index + 1,
                total,
                email,
                response.status_code,
            )
            return email, True, None
        except Exception as exc:
            logger.exception(
                "n8n invoice webhook failed (%d/%d): email=%s",
                index + 1,
                total,
                email,
            )
            return email, False, str(exc)


async def trigger_supplier_invoices_batch(
    supplier_emails: list[str] | None = None,
) -> dict[str, Any]:
    """POST supplier emails to the n8n invoice webhook concurrently.

    If ``supplier_emails`` is None or empty, emails are loaded from the Monday
    suppliers board. Returns a summary dict with counts and per-email errors.
    """
    webhook_url = _webhook_url()
    if not webhook_url:
        logger.warning(
            "Supplier invoice batch skipped: N8N_INVOICE_WEBHOOK_URL not configured"
        )
        return _empty_summary(reason="webhook_not_configured")

    raw_emails = [e for e in (supplier_emails or []) if e and e.strip()]
    emails_found = 0
    emails: list[str] = []

    if raw_emails:
        emails_found = len(raw_emails)
        seen: set[str] = set()
        for email in (normalize_email(e) for e in raw_emails):
            if email and email not in seen:
                seen.add(email)
                emails.append(email)
    else:
        try:
            emails, emails_found = await fetch_supplier_emails_from_monday()
        except Exception as exc:
            logger.exception("Failed to fetch supplier emails from Monday")
            return {
                "status": "error",
                "reason": "monday_fetch_failed",
                "emails_found": 0,
                "unique": 0,
                "total": 0,
                "triggered": 0,
                "failed": 0,
                "skipped": 0,
                "errors": [{"email": None, "error": str(exc)}],
            }

    unique_count = len(emails)
    if not emails:
        logger.info("Supplier invoice batch: no supplier emails to process")
        summary = _empty_summary(reason="no_emails", status="ok")
        summary["emails_found"] = emails_found
        summary["unique"] = 0
        return summary

    delay = _batch_delay_seconds()
    concurrency = _batch_concurrency()
    limit = concurrency if concurrency > 0 else unique_count
    semaphore = asyncio.Semaphore(limit)

    logger.info(
        "Supplier invoice batch starting: emails_found=%d unique=%d "
        "concurrency=%s stagger=%.2fs webhook=%s",
        emails_found,
        unique_count,
        concurrency if concurrency > 0 else "unlimited",
        delay,
        webhook_url,
    )

    async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
        results = await asyncio.gather(
            *[
                _trigger_one_webhook(
                    client,
                    webhook_url,
                    email,
                    index=index,
                    total=unique_count,
                    semaphore=semaphore,
                    start_delay=index * delay,
                )
                for index, email in enumerate(emails)
            ]
        )

    triggered = 0
    failed = 0
    errors: list[dict[str, str | None]] = []
    for email, ok, error in results:
        if ok:
            triggered += 1
        else:
            failed += 1
            errors.append({"email": email, "error": error})

    summary: dict[str, Any] = {
        "status": "ok" if failed == 0 else "partial" if triggered > 0 else "error",
        "emails_found": emails_found,
        "unique": unique_count,
        "total": unique_count,
        "triggered": triggered,
        "failed": failed,
        "skipped": 0,
        "errors": errors,
    }
    logger.info(
        "Supplier invoice batch finished: emails_found=%d unique=%d "
        "triggered=%d failed=%d status=%s",
        summary["emails_found"],
        summary["unique"],
        summary["triggered"],
        summary["failed"],
        summary["status"],
    )
    return summary
