"""
Relocate Beyond Code company meetings that were wrongly migrated as lead subitems
under ביונד קוד בע"מ back to the company meetings board.

Usage:
    python relocate_beyondcode_subitems_to_company_board.py --dry-run
    python relocate_beyondcode_subitems_to_company_board.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import ValidationError

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.meeting import (
    _column_by_id,
    _people_column_value,
    column_text,
    create_meeting_item,
    date_column_value,
    parse_comma_separated_emails,
    resolve_beyondcode_client_match,
)
from crm_integration.monday_client import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    DELETE_ITEM_MUTATION,
    FIND_ITEMS_LIMIT,
    ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
    execute_graphql,
    fetch_item_doc_id,
    fetch_items_by_ids,
)
from crm_integration.schemas import NodeTakerWebhookPayload
from crm_integration.workdoc import copy_workdoc_blocks
from migrate_meeting_notes_to_subitems import (
    MIGRATION_DELAY_SECONDS,
    _extract_participant_emails,
    _retry_item_step,
    _valid_email,
)
from services.monday_service import normalize_email

_BACKEND_ROOT = Path(__file__).resolve().parent
REPORT_PATH = _BACKEND_ROOT / "relocate_beyondcode_report.json"

logger = logging.getLogger(__name__)

RelocateStatus = Literal[
    "relocated",
    "skipped_dry_run",
    "skipped_duplicate_deleted_subitem",
    "skipped_subitem_missing",
    "error",
]


@dataclass
class RelocateRowResult:
    subitem_id: str
    title: str
    status: RelocateStatus
    company_item_id: str | None = None
    workdoc_strategy: str | None = None
    error: str | None = None


@dataclass
class RelocateReport:
    dry_run: bool
    total_rows: int = 0
    relocated: int = 0
    skipped_dry_run: int = 0
    skipped_duplicate_deleted_subitem: int = 0
    skipped_subitem_missing: int = 0
    errors: int = 0
    workdoc_copied: int = 0
    rows: list[RelocateRowResult] = field(default_factory=list)

    def record(self, row: RelocateRowResult) -> None:
        self.rows.append(row)
        if row.status == "relocated":
            self.relocated += 1
            if row.workdoc_strategy == "copied":
                self.workdoc_copied += 1
        elif row.status == "skipped_dry_run":
            self.skipped_dry_run += 1
        elif row.status == "skipped_duplicate_deleted_subitem":
            self.skipped_duplicate_deleted_subitem += 1
        elif row.status == "skipped_subitem_missing":
            self.skipped_subitem_missing += 1
        elif row.status == "error":
            self.errors += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "total_rows": self.total_rows,
            "relocated": self.relocated,
            "skipped_dry_run": self.skipped_dry_run,
            "skipped_duplicate_deleted_subitem": self.skipped_duplicate_deleted_subitem,
            "skipped_subitem_missing": self.skipped_subitem_missing,
            "errors": self.errors,
            "workdoc_copied": self.workdoc_copied,
            "rows": [
                {
                    "subitem_id": row.subitem_id,
                    "title": row.title,
                    "status": row.status,
                    "company_item_id": row.company_item_id,
                    "workdoc_strategy": row.workdoc_strategy,
                    "error": row.error,
                }
                for row in self.rows
            ],
        }


def _persist_report(report: RelocateReport) -> None:
    REPORT_PATH.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_beyondcode_subitem_ids(settings: CrmSettings) -> list[dict[str, str]]:
    supplement_path = _BACKEND_ROOT / "migration_board_relation_report.json"
    if not supplement_path.exists():
        raise FileNotFoundError(f"Supplement report not found: {supplement_path}")

    data = json.loads(supplement_path.read_text(encoding="utf-8"))
    beyondcode_lead_id = settings.beyondcode_company_client_item_id.strip()
    rows: list[dict[str, str]] = []
    for row in data.get("rows") or []:
        if row.get("status") != "migrated":
            continue
        if str(row.get("lead_item_id") or "") != beyondcode_lead_id:
            continue
        subitem_id = str(row.get("subitem_id") or "").strip()
        if not subitem_id:
            continue
        rows.append(
            {
                "subitem_id": subitem_id,
                "title": str(row.get("title") or ""),
                "legacy_item_id": str(row.get("legacy_item_id") or ""),
            }
        )
    return rows


def _subitem_column_ids(settings: CrmSettings) -> list[str]:
    return [
        settings.monday_crm_lead_subitem_date_column_id,
        settings.monday_crm_lead_subitem_summary_column_id,
        settings.monday_crm_lead_subitem_action_items_column_id,
        settings.monday_crm_lead_subitem_external_participants_column_id,
        settings.monday_crm_lead_subitem_people_column_id,
        settings.monday_crm_lead_subitem_doc_column_id,
    ]


def _people_user_ids_from_column(column: dict[str, Any] | None) -> list[str]:
    if not column:
        return []
    value = column.get("value")
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    user_ids: list[str] = []
    for entry in parsed.get("personsAndTeams") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "").lower() != "person":
            continue
        user_id = entry.get("id")
        if user_id is not None:
            user_ids.append(str(user_id))
    return user_ids


def _payload_from_subitem(
    item: dict[str, Any],
    settings: CrmSettings,
) -> tuple[NodeTakerWebhookPayload | None, str | None]:
    title = str(item.get("name") or "").strip()
    if not title:
        return None, "Missing subitem name"

    date_col = _column_by_id(item, settings.monday_crm_lead_subitem_date_column_id)
    meeting_date = date_column_value(date_col or {})
    if meeting_date is None:
        return None, "Missing or invalid meeting date"

    summary_col = _column_by_id(item, settings.monday_crm_lead_subitem_summary_column_id)
    action_items_col = _column_by_id(item, settings.monday_crm_lead_subitem_action_items_column_id)
    external_col = _column_by_id(item, settings.monday_crm_lead_subitem_external_participants_column_id)

    summary = column_text(summary_col or {})
    action_items = column_text(action_items_col or {})
    external_raw = column_text(external_col or {})
    participant_emails = _extract_participant_emails(external_raw)
    if not participant_emails:
        participant_emails = [
            validated
            for email in parse_comma_separated_emails(external_raw)
            if (validated := _valid_email(normalize_email(email) or ""))
        ]

    try:
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "participant_emails": participant_emails,
                "meeting_summary": summary,
                "action_items": action_items,
            }
        )
    except ValidationError as exc:
        return None, str(exc)

    return payload, None


async def _find_company_item_id(
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
) -> str | None:
    body = await execute_graphql(
        ITEMS_PAGE_BY_COLUMN_VALUES_QUERY,
        {
            "boardId": settings.monday_crm_company_meetings_board_id,
            "limit": FIND_ITEMS_LIMIT,
            "columns": [
                {
                    "column_id": settings.monday_crm_meeting_date_column_id,
                    "column_values": [payload.meeting_date.isoformat()],
                }
            ],
        },
        column_ids=[settings.monday_crm_meeting_date_column_id],
    )
    items = body.get("data", {}).get("items_page_by_column_values", {}).get("items") or []
    title = payload.meeting_title.strip()
    for item in items:
        if str(item.get("name") or "").strip() == title:
            return str(item.get("id"))
    return None


async def _delete_subitem(subitem_id: str) -> None:
    body = await execute_graphql(DELETE_ITEM_MUTATION, {"itemId": int(subitem_id)})
    deleted_id = body.get("data", {}).get("delete_item", {}).get("id")
    if not deleted_id:
        raise RuntimeError(f"delete_item returned no id for subitem {subitem_id}")


async def _apply_people_column(
    company_item_id: str,
    people_user_ids: list[str],
    settings: CrmSettings,
) -> None:
    if not people_user_ids:
        return
    await execute_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": settings.monday_crm_company_meetings_board_id,
            "itemId": int(company_item_id),
            "columnValues": json.dumps(
                {
                    settings.monday_crm_meeting_people_column_id: _people_column_value(
                        people_user_ids
                    )
                }
            ),
        },
        column_ids=[settings.monday_crm_meeting_people_column_id],
    )


async def _relocate_subitem(
    *,
    subitem_id: str,
    title: str,
    settings: CrmSettings,
    dry_run: bool,
) -> RelocateRowResult:
    items = await fetch_items_by_ids([subitem_id], _subitem_column_ids(settings))
    if not items:
        return RelocateRowResult(
            subitem_id=subitem_id,
            title=title,
            status="skipped_subitem_missing",
            error="Subitem not found in Monday",
        )

    item = items[0]
    payload, payload_error = _payload_from_subitem(item, settings)
    if payload is None:
        return RelocateRowResult(
            subitem_id=subitem_id,
            title=title,
            status="error",
            error=payload_error,
        )

    people_user_ids = _people_user_ids_from_column(
        _column_by_id(item, settings.monday_crm_lead_subitem_people_column_id)
    )
    existing_company_item_id = await _find_company_item_id(payload, settings)

    if dry_run:
        action = "would relocate"
        if existing_company_item_id:
            action = f"would delete subitem (company item {existing_company_item_id} exists)"
        return RelocateRowResult(
            subitem_id=subitem_id,
            title=title,
            status="skipped_dry_run",
            company_item_id=existing_company_item_id,
            error=action,
        )

    if existing_company_item_id:
        await _delete_subitem(subitem_id)
        return RelocateRowResult(
            subitem_id=subitem_id,
            title=title,
            status="skipped_duplicate_deleted_subitem",
            company_item_id=existing_company_item_id,
        )

    beyondcode_match = await resolve_beyondcode_client_match(settings)
    company_item_id = await create_meeting_item(
        payload,
        beyondcode_match,
        settings=settings,
        board_kind="company",
    )
    await _apply_people_column(company_item_id, people_user_ids, settings)

    workdoc_strategy: str | None = None
    source_doc_id = await fetch_item_doc_id(
        subitem_id,
        settings.monday_crm_lead_subitem_doc_column_id,
    )
    if source_doc_id:
        _, doc_created, doc_warnings = await copy_workdoc_blocks(
            source_doc_id,
            company_item_id,
            settings=settings,
            board_kind="company",
        )
        for warning in doc_warnings:
            logger.warning("Workdoc warning for subitem %s: %s", subitem_id, warning)
        if doc_created:
            workdoc_strategy = "copied"

    await _delete_subitem(subitem_id)
    return RelocateRowResult(
        subitem_id=subitem_id,
        title=title,
        status="relocated",
        company_item_id=company_item_id,
        workdoc_strategy=workdoc_strategy,
    )


async def run_relocation(*, dry_run: bool) -> RelocateReport:
    settings = get_crm_settings()
    candidates = _load_beyondcode_subitem_ids(settings)
    report = RelocateReport(dry_run=dry_run, total_rows=len(candidates))

    print(f"Relocating {len(candidates)} Beyond Code subitems to company meetings board...")
    for index, candidate in enumerate(candidates, start=1):
        subitem_id = candidate["subitem_id"]
        title = candidate["title"] or subitem_id
        print(f"[{index}/{len(candidates)}] {title} (subitem {subitem_id})")

        try:
            row = await _retry_item_step(
                f"relocate subitem {subitem_id}",
                lambda subitem_id=subitem_id, title=title: _relocate_subitem(
                    subitem_id=subitem_id,
                    title=title,
                    settings=settings,
                    dry_run=dry_run,
                ),
            )
        except Exception as exc:
            logger.exception("Failed to relocate subitem %s", subitem_id)
            row = RelocateRowResult(
                subitem_id=subitem_id,
                title=title,
                status="error",
                error=str(exc),
            )

        report.record(row)
        _persist_report(report)

        if row.status == "relocated":
            print(
                f"  -> relocated to company item {row.company_item_id}"
                f"{f' (workdoc {row.workdoc_strategy})' if row.workdoc_strategy else ''}"
            )
        elif row.status == "skipped_duplicate_deleted_subitem":
            print(f"  -> deleted subitem; company item {row.company_item_id} already existed")
        elif row.status == "skipped_dry_run":
            print(f"  -> dry-run: {row.error}")
        elif row.status == "skipped_subitem_missing":
            print(f"  -> skipped: {row.error}")
        else:
            print(f"  -> error: {row.error}")

        if not dry_run:
            await asyncio.sleep(MIGRATION_DELAY_SECONDS)

    return report


def _print_summary(report: RelocateReport) -> None:
    print("\n=== Beyond Code Relocation Summary ===")
    print(f"Candidates:            {report.total_rows}")
    print(f"Relocated:             {report.relocated}")
    print(f"Workdocs copied:       {report.workdoc_copied}")
    print(f"Duplicate cleanup:     {report.skipped_duplicate_deleted_subitem}")
    print(f"Subitem missing:       {report.skipped_subitem_missing}")
    print(f"Dry-run only:          {report.skipped_dry_run}")
    print(f"Errors:                {report.errors}")
    print(f"Report:                {REPORT_PATH}")


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Move Beyond Code meetings from lead subitems to the company meetings board."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    try:
        report = asyncio.run(run_relocation(dry_run=args.dry_run))
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _print_summary(report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
