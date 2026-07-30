"""
One-off migration: copy all rows from the legacy Meeting Notes board into Lead Subitems.

Usage:
    python migrate_meeting_notes_to_subitems.py --dry-run
    python migrate_meeting_notes_to_subitems.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import EmailStr, TypeAdapter, ValidationError

from crm_integration.config import CrmSettings, get_crm_settings
from crm_integration.lookup import ContactMatch, find_contact_by_emails
from crm_integration.meeting import (
    MeetingTypeLabel,
    _column_by_id,
    column_text,
    create_meeting_subitem,
    date_column_value,
    meeting_subitem_already_exists,
    parse_board_relation_lead_id,
    parse_comma_separated_emails,
    resolve_meeting_type_label,
)
from crm_integration.monday_client import fetch_all_board_items, fetch_item_doc_id, execute_graphql, ITEM_SUBITEMS_WITH_COLUMNS_QUERY
from crm_integration.schemas import NodeTakerWebhookPayload
from crm_integration.workdoc import copy_workdoc_blocks, create_meeting_workdoc
from services.monday_service import normalize_email

_BACKEND_ROOT = Path(__file__).resolve().parent
REPORT_PATH = _BACKEND_ROOT / "migration_meeting_notes_report.json"
MIGRATION_DELAY_SECONDS = 0.1
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9._-]+\.[A-Za-z]{2,}")
EMAIL_VALIDATOR = TypeAdapter(EmailStr)
MAX_ITEM_RETRIES = 3

logger = logging.getLogger(__name__)

MigrationStatus = Literal[
    "migrated",
    "skipped_duplicate",
    "skipped_no_lead",
    "skipped_no_date",
    "skipped_dry_run",
    "workdoc_repaired",
    "error",
]


@dataclass
class MigrationRowResult:
    legacy_item_id: str
    title: str
    status: MigrationStatus
    lead_item_id: str | None = None
    subitem_id: str | None = None
    workdoc_strategy: str | None = None
    match_source: str | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationRowResult:
        return cls(
            legacy_item_id=str(data["legacy_item_id"]),
            title=str(data.get("title") or ""),
            status=data["status"],
            lead_item_id=data.get("lead_item_id"),
            subitem_id=data.get("subitem_id"),
            workdoc_strategy=data.get("workdoc_strategy"),
            match_source=data.get("match_source"),
            error=data.get("error"),
        )


def _row_key(row: MigrationRowResult) -> str:
    return row.legacy_item_id


def _persist_report(report: MigrationReport) -> None:
    REPORT_PATH.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_resumed_report(*, dry_run: bool) -> MigrationReport | None:
    if dry_run or not REPORT_PATH.exists():
        return None
    try:
        data = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("dry_run"):
        return None

    report = MigrationReport(dry_run=False, total_legacy_rows=int(data.get("total_legacy_rows") or 0))
    for row_data in data.get("rows") or []:
        report.record(MigrationRowResult.from_dict(row_data))
    return report


def _checkpoint_index(report: MigrationReport) -> dict[str, MigrationRowResult]:
    return {_row_key(row): row for row in report.rows}


def _should_skip_checkpoint(
    row: MigrationRowResult | None,
    *,
    repair_workdocs: bool,
) -> bool:
    if row is None:
        return False
    if row.status == "error":
        return False
    if row.status in {"skipped_no_lead", "skipped_no_date"}:
        return True
    if row.status == "workdoc_repaired":
        return True
    if row.status == "migrated" and row.workdoc_strategy:
        return True
    if row.status == "skipped_duplicate" and not repair_workdocs:
        return True
    if repair_workdocs and row.status in {"migrated", "skipped_duplicate"}:
        return bool(row.workdoc_strategy)
    return row.status == "migrated"


def _upsert_report_row(report: MigrationReport, row: MigrationRowResult) -> None:
    index = _checkpoint_index(report)
    previous = index.get(_row_key(row))
    if previous is not None:
        report.rows = [existing for existing in report.rows if _row_key(existing) != _row_key(row)]
        if previous.status == "migrated":
            report.migrated -= 1
            if previous.workdoc_strategy == "copied":
                report.workdoc_copied -= 1
            elif previous.workdoc_strategy == "rebuilt":
                report.workdoc_rebuilt -= 1
        elif previous.status == "skipped_duplicate":
            report.skipped_duplicate -= 1
        elif previous.status == "skipped_no_lead":
            report.skipped_no_lead -= 1
        elif previous.status == "skipped_no_date":
            report.skipped_no_date -= 1
        elif previous.status == "skipped_dry_run":
            report.skipped_dry_run -= 1
        elif previous.status == "workdoc_repaired":
            report.workdoc_repaired -= 1
            if previous.workdoc_strategy == "copied":
                report.workdoc_copied -= 1
            elif previous.workdoc_strategy == "rebuilt":
                report.workdoc_rebuilt -= 1
        elif previous.status == "error":
            report.errors -= 1
    report.record(row)
    _persist_report(report)


async def _retry_item_step(
    label: str,
    operation,
):
    last_error: Exception | None = None
    for attempt in range(1, MAX_ITEM_RETRIES + 1):
        try:
            return await operation()
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_ITEM_RETRIES:
                break
            logger.warning("%s failed (attempt %d/%d): %s", label, attempt, MAX_ITEM_RETRIES, exc)
            await asyncio.sleep(attempt)
    assert last_error is not None
    raise last_error


@dataclass
class MigrationReport:
    dry_run: bool
    total_legacy_rows: int = 0
    migrated: int = 0
    skipped_duplicate: int = 0
    skipped_no_lead: int = 0
    skipped_no_date: int = 0
    skipped_dry_run: int = 0
    workdoc_copied: int = 0
    workdoc_rebuilt: int = 0
    workdoc_repaired: int = 0
    errors: int = 0
    rows: list[MigrationRowResult] = field(default_factory=list)

    def record(self, row: MigrationRowResult) -> None:
        self.rows.append(row)
        if row.status == "migrated":
            self.migrated += 1
            if row.workdoc_strategy == "copied":
                self.workdoc_copied += 1
            elif row.workdoc_strategy == "rebuilt":
                self.workdoc_rebuilt += 1
        elif row.status == "skipped_duplicate":
            self.skipped_duplicate += 1
        elif row.status == "skipped_no_lead":
            self.skipped_no_lead += 1
        elif row.status == "skipped_no_date":
            self.skipped_no_date += 1
        elif row.status == "skipped_dry_run":
            self.skipped_dry_run += 1
        elif row.status == "workdoc_repaired":
            self.workdoc_repaired += 1
            if row.workdoc_strategy == "copied":
                self.workdoc_copied += 1
            elif row.workdoc_strategy == "rebuilt":
                self.workdoc_rebuilt += 1
        elif row.status == "error":
            self.errors += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "total_legacy_rows": self.total_legacy_rows,
            "migrated": self.migrated,
            "skipped_duplicate": self.skipped_duplicate,
            "skipped_no_lead": self.skipped_no_lead,
            "skipped_no_date": self.skipped_no_date,
            "skipped_dry_run": self.skipped_dry_run,
            "workdoc_copied": self.workdoc_copied,
            "workdoc_rebuilt": self.workdoc_rebuilt,
            "workdoc_repaired": self.workdoc_repaired,
            "errors": self.errors,
            "rows": [
                {
                    "legacy_item_id": row.legacy_item_id,
                    "title": row.title,
                    "status": row.status,
                    "lead_item_id": row.lead_item_id,
                    "subitem_id": row.subitem_id,
                    "workdoc_strategy": row.workdoc_strategy,
                    "match_source": row.match_source,
                    "error": row.error,
                }
                for row in self.rows
            ],
        }


def _valid_email(email: str) -> str | None:
    try:
        return str(EMAIL_VALIDATOR.validate_python(email))
    except ValidationError:
        return None


def _extract_participant_emails(raw: str) -> list[str]:
    """Extract valid emails from legacy participant text (supports 'Name (email)' format)."""
    emails: list[str] = []
    seen: set[str] = set()

    for part in re.split(r"[,;]", raw):
        part = part.strip()
        if not part:
            continue
        candidates = EMAIL_PATTERN.findall(part) or ([part] if "@" in part else [])
        for candidate in candidates:
            normalized = normalize_email(candidate)
            validated = _valid_email(normalized) if normalized else None
            if validated and validated not in seen:
                seen.add(validated)
                emails.append(validated)
    return emails


def _legacy_column_ids(settings: CrmSettings) -> list[str]:
    column_ids = [
        settings.monday_crm_meeting_date_column_id,
        settings.monday_crm_meeting_summary_column_id,
        settings.monday_crm_meeting_action_items_column_id,
        settings.monday_crm_meeting_type_column_id,
        settings.monday_crm_meeting_external_participants_column_id,
        settings.monday_crm_meeting_doc_column_id,
        settings.meeting_notes_reminder_date_column_id,
        settings.meeting_notes_reminder_info_column_id,
    ]
    if settings.monday_crm_meeting_lead_relation_column_id:
        column_ids.append(settings.monday_crm_meeting_lead_relation_column_id)
    return column_ids


def _legacy_reminder_from_item(
    item: dict[str, Any],
    settings: CrmSettings,
) -> dict[str, str] | None:
    reminder_date_col = _column_by_id(item, settings.meeting_notes_reminder_date_column_id)
    reminder_info_col = _column_by_id(item, settings.meeting_notes_reminder_info_column_id)

    reminder_date = date_column_value(reminder_date_col or {})
    reminder_info = column_text(reminder_info_col or {})

    if not reminder_date and not reminder_info:
        return None

    reminder: dict[str, str] = {}
    if reminder_date:
        reminder["date"] = reminder_date.isoformat()
    if reminder_info:
        reminder["info"] = reminder_info
    return reminder


def _payload_from_legacy_item(
    item: dict[str, Any],
    settings: CrmSettings,
) -> tuple[NodeTakerWebhookPayload | None, MeetingTypeLabel, str | None]:
    title = str(item.get("name") or "").strip()
    if not title:
        return None, "מעקב", "Missing item name"

    date_col = _column_by_id(item, settings.monday_crm_meeting_date_column_id)
    meeting_date = date_column_value(date_col or {})
    if meeting_date is None:
        return None, "מעקב", "Missing or invalid meeting date"

    summary_col = _column_by_id(item, settings.monday_crm_meeting_summary_column_id)
    action_items_col = _column_by_id(item, settings.monday_crm_meeting_action_items_column_id)
    external_col = _column_by_id(item, settings.monday_crm_meeting_external_participants_column_id)
    type_col = _column_by_id(item, settings.monday_crm_meeting_type_column_id)

    summary = column_text(summary_col or {})
    action_items = column_text(action_items_col or {})
    external_raw = column_text(external_col or {})
    external_emails = _extract_participant_emails(external_raw)
    if not external_emails:
        external_emails = [
            validated
            for email in parse_comma_separated_emails(external_raw)
            if (validated := _valid_email(normalize_email(email) or ""))
        ]
    legacy_type_text = column_text(type_col or {})
    meeting_type = resolve_meeting_type_label(legacy_type_text, title, summary)

    try:
        payload = NodeTakerWebhookPayload.model_validate(
            {
                "meeting_title": title,
                "meeting_date": meeting_date.isoformat(),
                "participant_emails": external_emails,
                "meeting_summary": summary,
                "action_items": action_items,
            }
        )
    except ValidationError as exc:
        return None, meeting_type, str(exc)

    return payload, meeting_type, None


async def _resolve_lead_match(
    item: dict[str, Any],
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
) -> tuple[ContactMatch | None, str | None]:
    relation_col = None
    if settings.monday_crm_meeting_lead_relation_column_id:
        relation_col = _column_by_id(item, settings.monday_crm_meeting_lead_relation_column_id)

    lead_id = parse_board_relation_lead_id(relation_col)
    if lead_id:
        return ContactMatch(
            item_id=lead_id,
            match_type="lead",
            matched_email="",
        ), "board_relation"

    external_emails = [str(email) for email in payload.participant_emails]
    if not external_emails:
        return None, None

    match = await find_contact_by_emails(external_emails, settings=settings)
    if match:
        return match, "participant_email"
    return None, None


async def _find_existing_subitem_id(
    payload: NodeTakerWebhookPayload,
    lead_item_id: str,
    settings: CrmSettings,
) -> str | None:
    date_column_id = settings.monday_crm_lead_subitem_date_column_id
    body = await execute_graphql(
        ITEM_SUBITEMS_WITH_COLUMNS_QUERY,
        {"itemId": lead_item_id, "columnIds": [date_column_id]},
        column_ids=[date_column_id],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return None

    title = payload.meeting_title.strip()
    for subitem in items[0].get("subitems") or []:
        if str(subitem.get("name") or "").strip() != title:
            continue
        date_column = _column_by_id(subitem, date_column_id)
        meeting_date = date_column_value(date_column or {})
        if meeting_date == payload.meeting_date:
            subitem_id = subitem.get("id")
            return str(subitem_id) if subitem_id is not None else None
    return None


async def _attach_workdoc(
    legacy_item_id: str,
    subitem_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
) -> tuple[str | None, list[str]]:
    source_doc_id = await fetch_item_doc_id(
        legacy_item_id,
        settings.monday_crm_meeting_doc_column_id,
    )
    if source_doc_id:
        doc_id, doc_created, warnings = await copy_workdoc_blocks(
            source_doc_id,
            subitem_id,
            settings,
            board_kind="subitem",
        )
        if doc_created and doc_id:
            return "copied", warnings
        if warnings:
            for warning in warnings:
                logger.warning("Workdoc copy failed for %s: %s", legacy_item_id, warning)

    doc_id, doc_created, warnings = await create_meeting_workdoc(
        subitem_id,
        payload,
        settings,
        board_kind="subitem",
    )
    if doc_created and doc_id:
        return "rebuilt", warnings
    return None, warnings


async def migrate_meeting_notes(*, dry_run: bool, repair_workdocs: bool = False) -> MigrationReport:
    settings = get_crm_settings()
    board_id = settings.monday_crm_meeting_notes_board_id.strip()
    if not board_id:
        raise ValueError(
            "MONDAY_CRM_MEETING_NOTES_BOARD_ID must be set in .env before running migration"
        )

    column_ids = _legacy_column_ids(settings)
    print(f"Fetching all items from legacy Meeting Notes board {board_id}...")
    legacy_items = await fetch_all_board_items(board_id, column_ids)

    resumed = _load_resumed_report(dry_run=dry_run)
    report = resumed or MigrationReport(dry_run=dry_run, total_legacy_rows=len(legacy_items))
    report.total_legacy_rows = len(legacy_items)
    checkpoint = _checkpoint_index(report)
    if resumed:
        print(f"Resuming from checkpoint with {len(checkpoint)} processed rows.")

    print(f"Found {len(legacy_items)} legacy rows.")

    for index, item in enumerate(legacy_items, start=1):
        legacy_item_id = str(item.get("id") or "")
        title = str(item.get("name") or "").strip() or "(untitled)"
        print(f"[{index}/{len(legacy_items)}] Processing {legacy_item_id}: {title!r}")

        existing = checkpoint.get(legacy_item_id)
        if _should_skip_checkpoint(existing, repair_workdocs=repair_workdocs):
            print(f"  -> checkpoint: {existing.status}")
            await asyncio.sleep(MIGRATION_DELAY_SECONDS)
            continue

        try:
            async def run_item() -> MigrationRowResult:
                return await _process_legacy_item(
                    item=item,
                    title=title,
                    legacy_item_id=legacy_item_id,
                    settings=settings,
                    dry_run=dry_run,
                    repair_workdocs=repair_workdocs,
                )

            row = await _retry_item_step(
                f"legacy item {legacy_item_id}",
                run_item,
            )
        except Exception as exc:
            row = MigrationRowResult(
                legacy_item_id=legacy_item_id,
                title=title,
                status="error",
                error=str(exc) or repr(exc),
            )
            print(f"  -> error: {row.error}")
            logger.exception("Failed to migrate legacy item %s", legacy_item_id)

        _upsert_report_row(report, row)
        checkpoint[_row_key(row)] = row
        if row.status == "migrated":
            print(
                f"  -> migrated to subitem {row.subitem_id} under lead {row.lead_item_id}"
                + (f" (workdoc {row.workdoc_strategy})" if row.workdoc_strategy else " (no workdoc)")
            )
        elif row.status == "workdoc_repaired":
            print(
                f"  -> repaired workdoc on subitem {row.subitem_id} ({row.workdoc_strategy})"
            )
        elif row.status == "skipped_duplicate":
            print(f"  -> skipped: duplicate subitem under lead {row.lead_item_id}")
        elif row.status == "skipped_no_lead":
            print("  -> skipped: no lead match")
        elif row.status == "skipped_no_date":
            print(f"  -> skipped: {row.error}")
        elif row.status == "skipped_dry_run":
            print(f"  -> dry-run: would migrate to lead {row.lead_item_id}")

        await asyncio.sleep(MIGRATION_DELAY_SECONDS)

    return report


async def _process_legacy_item(
    *,
    item: dict[str, Any],
    title: str,
    legacy_item_id: str,
    settings: CrmSettings,
    dry_run: bool,
    repair_workdocs: bool,
) -> MigrationRowResult:
    payload, meeting_type, payload_error = _payload_from_legacy_item(item, settings)
    if payload is None:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_no_date" if "date" in (payload_error or "").lower() else "error",
            error=payload_error,
        )

    match, match_source = await _resolve_lead_match(item, payload, settings)
    if not match or not match.item_id:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_no_lead",
        )

    if await meeting_subitem_already_exists(payload, match.item_id, settings=settings):
        if repair_workdocs and not dry_run:
            subitem_id = await _find_existing_subitem_id(payload, match.item_id, settings)
            if subitem_id:
                workdoc_strategy, workdoc_warnings = await _attach_workdoc(
                    legacy_item_id,
                    subitem_id,
                    payload,
                    settings,
                )
                if workdoc_strategy:
                    return MigrationRowResult(
                        legacy_item_id=legacy_item_id,
                        title=title,
                        status="workdoc_repaired",
                        lead_item_id=match.item_id,
                        subitem_id=subitem_id,
                        workdoc_strategy=workdoc_strategy,
                        match_source=match_source,
                    )
                for warning in workdoc_warnings:
                    logger.warning("Workdoc repair failed for %s: %s", legacy_item_id, warning)

        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_duplicate",
            lead_item_id=match.item_id,
            match_source=match_source,
        )

    if dry_run:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_dry_run",
            lead_item_id=match.item_id,
            match_source=match_source,
        )

    reminder = _legacy_reminder_from_item(item, settings)
    subitem_id = await create_meeting_subitem(
        payload,
        match,
        settings=settings,
        meeting_type_override=meeting_type,
        reminder=reminder,
        fetch_mirly_reminder=False,
    )
    workdoc_strategy, workdoc_warnings = await _attach_workdoc(
        legacy_item_id,
        subitem_id,
        payload,
        settings,
    )
    for warning in workdoc_warnings:
        logger.warning("Workdoc warning for %s: %s", legacy_item_id, warning)

    return MigrationRowResult(
        legacy_item_id=legacy_item_id,
        title=title,
        status="migrated",
        lead_item_id=match.item_id,
        subitem_id=subitem_id,
        workdoc_strategy=workdoc_strategy,
        match_source=match_source,
    )


def _print_summary(report: MigrationReport) -> None:
    print("\n=== Migration Summary ===")
    print(f"Total legacy rows:     {report.total_legacy_rows}")
    if report.dry_run:
        print(f"Would migrate:         {report.skipped_dry_run}")
    else:
        print(f"Migrated:              {report.migrated}")
    print(f"Skipped (duplicate):   {report.skipped_duplicate}")
    print(f"Skipped (no lead):     {report.skipped_no_lead}")
    print(f"Skipped (no date):     {report.skipped_no_date}")
    if report.dry_run:
        print(f"Dry-run previews:      {report.skipped_dry_run}")
    print(f"Workdoc copied:        {report.workdoc_copied}")
    print(f"Workdoc rebuilt:       {report.workdoc_rebuilt}")
    print(f"Workdoc repaired:      {report.workdoc_repaired}")
    print(f"Errors:                {report.errors}")
    print(f"Report written to:     {REPORT_PATH}")


def main() -> int:
    load_dotenv(_BACKEND_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Migrate legacy Meeting Notes board rows to Lead Subitems.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without creating subitems or workdocs.",
    )
    parser.add_argument(
        "--repair-workdocs",
        action="store_true",
        help="When a matching subitem already exists, attempt to copy/rebuild its Workdoc.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    report: MigrationReport | None = None
    try:
        report = asyncio.run(
            migrate_meeting_notes(
                dry_run=args.dry_run,
                repair_workdocs=args.repair_workdocs,
            )
        )
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if report is not None:
            _persist_report(report)

    if report is None:
        return 1

    _print_summary(report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
