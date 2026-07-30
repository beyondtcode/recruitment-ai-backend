"""
Supplemental migration: transfer legacy Meeting Notes rows that have a board-relation
lead link but were skipped in the email-based migration (skipped_no_lead).

Reads skipped item IDs from migration_meeting_notes_report.json and re-fetches
board-relation column values using the BoardRelationValue GraphQL fragment.

Usage:
    python migrate_meeting_notes_board_relation.py --dry-run
    python migrate_meeting_notes_board_relation.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from crm_integration.config import get_crm_settings
from crm_integration.lookup import ContactMatch
from crm_integration.meeting import (
    _column_by_id,
    create_meeting_subitem,
    meeting_subitem_already_exists,
    parse_board_relation_lead_id,
)
from crm_integration.monday_client import fetch_items_by_ids
from migrate_meeting_notes_to_subitems import (
    MIGRATION_DELAY_SECONDS,
    MigrationReport,
    MigrationRowResult,
    REPORT_PATH,
    _attach_workdoc,
    _legacy_column_ids,
    _legacy_reminder_from_item,
    _payload_from_legacy_item,
    _retry_item_step,
)

_BACKEND_ROOT = Path(__file__).resolve().parent
SUPPLEMENT_REPORT_PATH = _BACKEND_ROOT / "migration_board_relation_report.json"
SOURCE_REPORT_PATH = REPORT_PATH


def _persist_supplement_report(report: MigrationReport) -> None:
    SUPPLEMENT_REPORT_PATH.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


logger = logging.getLogger(__name__)


def _load_skipped_no_lead_ids() -> list[str]:
    if not SOURCE_REPORT_PATH.exists():
        raise FileNotFoundError(
            f"Source report not found: {SOURCE_REPORT_PATH}. Run the main migration first."
        )
    data = json.loads(SOURCE_REPORT_PATH.read_text(encoding="utf-8"))
    return [
        str(row["legacy_item_id"])
        for row in data.get("rows") or []
        if row.get("status") == "skipped_no_lead" and row.get("legacy_item_id")
    ]


def _resolve_lead_from_board_relation(
    item: dict,
    settings,
) -> ContactMatch | None:
    relation_column_id = settings.monday_crm_meeting_lead_relation_column_id
    if not relation_column_id:
        return None

    relation_col = _column_by_id(item, relation_column_id)
    lead_id = parse_board_relation_lead_id(relation_col)
    if not lead_id:
        return None

    return ContactMatch(
        item_id=lead_id,
        match_type="lead",
        matched_email="",
    )


async def _migrate_board_relation_item(
    item: dict,
    *,
    settings,
    dry_run: bool,
) -> MigrationRowResult:
    legacy_item_id = str(item.get("id") or "")
    title = str(item.get("name") or "").strip() or "(untitled)"

    payload, meeting_type, payload_error = _payload_from_legacy_item(item, settings)
    if payload is None:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_no_date" if "date" in (payload_error or "").lower() else "error",
            error=payload_error,
        )

    match = _resolve_lead_from_board_relation(item, settings)
    if not match or not match.item_id:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_no_lead",
            error="No board-relation lead link found",
        )

    if await meeting_subitem_already_exists(payload, match.item_id, settings=settings):
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_duplicate",
            lead_item_id=match.item_id,
            match_source="board_relation",
        )

    if dry_run:
        return MigrationRowResult(
            legacy_item_id=legacy_item_id,
            title=title,
            status="skipped_dry_run",
            lead_item_id=match.item_id,
            match_source="board_relation",
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
        match_source="board_relation",
    )


async def migrate_board_relation_skipped(*, dry_run: bool) -> MigrationReport:
    settings = get_crm_settings()
    if not settings.monday_crm_meeting_lead_relation_column_id.strip():
        raise ValueError(
            "MONDAY_CRM_MEETING_LEAD_RELATION_COLUMN_ID must be set in .env"
        )

    skipped_ids = _load_skipped_no_lead_ids()
    report = MigrationReport(dry_run=dry_run, total_legacy_rows=len(skipped_ids))
    print(f"Re-processing {len(skipped_ids)} rows previously marked skipped_no_lead...")

    column_ids = _legacy_column_ids(settings)
    items = await fetch_items_by_ids(skipped_ids, column_ids)
    items_by_id = {str(item.get("id")): item for item in items}

    for index, legacy_item_id in enumerate(skipped_ids, start=1):
        item = items_by_id.get(legacy_item_id)
        title = str(item.get("name") or "").strip() if item else legacy_item_id
        print(f"[{index}/{len(skipped_ids)}] {legacy_item_id}: {title!r}")

        if not item:
            row = MigrationRowResult(
                legacy_item_id=legacy_item_id,
                title=title,
                status="error",
                error="Item not found on legacy board",
            )
            print(f"  -> error: {row.error}")
            report.record(row)
            _persist_supplement_report(report)
            await asyncio.sleep(MIGRATION_DELAY_SECONDS)
            continue

        try:
            async def run_item() -> MigrationRowResult:
                return await _migrate_board_relation_item(
                    item,
                    settings=settings,
                    dry_run=dry_run,
                )

            row = await _retry_item_step(
                f"board-relation item {legacy_item_id}",
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
            logger.exception("Failed board-relation migration for %s", legacy_item_id)

        report.record(row)
        _persist_supplement_report(report)
        if row.status == "migrated":
            print(
                f"  -> migrated to subitem {row.subitem_id} under lead {row.lead_item_id}"
                + (f" (workdoc {row.workdoc_strategy})" if row.workdoc_strategy else "")
            )
        elif row.status == "skipped_dry_run":
            print(f"  -> dry-run: would migrate to lead {row.lead_item_id}")
        elif row.status == "skipped_duplicate":
            print(f"  -> skipped: duplicate under lead {row.lead_item_id}")
        elif row.status == "skipped_no_lead":
            print("  -> skipped: still no board-relation lead")
        elif row.status == "skipped_no_date":
            print(f"  -> skipped: {row.error}")

        await asyncio.sleep(MIGRATION_DELAY_SECONDS)

    return report


def _print_summary(report: MigrationReport) -> None:
    print("\n=== Board-Relation Migration Summary ===")
    print(f"Candidates (skipped_no_lead): {report.total_legacy_rows}")
    if report.dry_run:
        print(f"Would migrate:         {report.skipped_dry_run}")
    else:
        print(f"Migrated:              {report.migrated}")
    print(f"Skipped (duplicate):   {report.skipped_duplicate}")
    print(f"Still no lead link:    {report.skipped_no_lead}")
    print(f"Skipped (no date):     {report.skipped_no_date}")
    print(f"Workdoc copied:        {report.workdoc_copied}")
    print(f"Workdoc rebuilt:       {report.workdoc_rebuilt}")
    print(f"Errors:                {report.errors}")
    print(f"Report written to:     {SUPPLEMENT_REPORT_PATH}")


def main() -> int:
    load_dotenv(_BACKEND_ROOT / ".env")

    parser = argparse.ArgumentParser(
        description="Migrate skipped_no_lead rows via board-relation column.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    report: MigrationReport | None = None
    try:
        report = asyncio.run(migrate_board_relation_skipped(dry_run=args.dry_run))
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if report is not None:
            _persist_supplement_report(report)

    if report is None:
        return 1

    _print_summary(report)
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
