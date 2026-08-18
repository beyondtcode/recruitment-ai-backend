"""Supplier invoice trigger routes.

Env vars:
  N8N_INVOICE_WEBHOOK_URL — n8n Webhook URL for invoice processing
  INVOICE_BATCH_CONCURRENCY — max concurrent webhook POSTs (default: 10)
  INVOICE_BATCH_DELAY_SECONDS — optional stagger between starts (default: 0)
  MONDAY_SUPPLIERS_BOARD_ID — board used when emails are not provided
  MONDAY_SUPPLIER_EMAIL_COLUMN_ID — email column on that board
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.supplier_invoices import trigger_supplier_invoices_batch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/invoices", tags=["invoices"])


class TriggerBatchRequest(BaseModel):
    emails: list[str] | None = Field(
        default=None,
        description=(
            "Optional supplier emails to trigger. If omitted or empty, "
            "emails are fetched from the Monday suppliers board."
        ),
    )


@router.post("/trigger-batch")
async def trigger_batch(
    background_tasks: BackgroundTasks,
    body: TriggerBatchRequest | None = None,
) -> JSONResponse:
    """Enqueue concurrent n8n invoice triggers; respond immediately with 202."""
    emails = body.emails if body is not None else None
    background_tasks.add_task(trigger_supplier_invoices_batch, emails)
    logger.info(
        "Accepted supplier invoice batch trigger (emails_provided=%s)",
        bool(emails),
    )
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "message": (
                "Supplier invoice batch started in the background. "
                "Supplier emails are posted to n8n concurrently."
            ),
        },
    )
