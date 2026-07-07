"""
Manual backfill runner for the Notetaker CRM batch.

Fetches Notetaker meetings from the past N days (default 5) and runs the full
CRM pipeline for each one against the LIVE Monday.com boards. Use this to
exercise the new subitem nesting verification (`_verify_subitem_nested`) and the
request/response logging in `create_meeting_subitem`.

Usage:
    python run_5_day_backfill.py
    python run_5_day_backfill.py --days 5
    python run_5_day_backfill.py --days 3
    python run_5_day_backfill.py --force
    python run_5_day_backfill.py --force --force-item-id 3019379608
    python run_5_day_backfill.py --force --force-item-id all

WARNING: This writes to production Monday boards (creates meeting subitems,
Workdocs, and updates AI profiles). It is not a dry run.

The --force flag bypasses the idempotency guard (`find_existing_meeting_subitem_id`)
so an already-backfilled meeting is re-created, letting you observe the new
`_verify_subitem_nested` logic and the raw Monday create_subitem response. By
default the bypass is scoped to a single lead item id (Eyal Almog, 3019379608)
to avoid duplicating unrelated meetings; pass `--force-item-id all` to bypass
every meeting in the window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv

DEFAULT_DAYS = 5
DEFAULT_FORCE_ITEM_ID = "3019379608"
FORCE_ALL_SENTINEL = "all"


def _configure_logging() -> None:
    """Send clean, timestamped logs to the console at INFO level."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # The verification/request-response logs we want to watch live here.
    logging.getLogger("crm_integration").setLevel(logging.INFO)


def _install_idempotency_bypass(force_item_id: str) -> None:
    """Force the idempotency guard to report 'no existing subitem' for testing.

    Both the pipeline (`find_existing_meeting_subitem_id`) and the batch guard
    (`meeting_subitem_already_exists`, which delegates to the same function in
    `crm_integration.meeting`) are patched so a fresh `create_meeting_subitem`
    is attempted. When ``force_item_id`` is a specific lead id, only that lead's
    meetings are bypassed; the sentinel ``"all"`` bypasses every meeting.
    """
    import crm_integration.meeting as meeting_module
    import crm_integration.pipeline as pipeline_module

    logger = logging.getLogger("run_5_day_backfill")
    real_find = meeting_module.find_existing_meeting_subitem_id

    scope = "ALL meetings" if force_item_id == FORCE_ALL_SENTINEL else f"lead item_id={force_item_id}"
    logger.warning(
        "IDEMPOTENCY GUARD BYPASS ENABLED (--force): forcing find_existing_meeting_subitem_id "
        "to return None for %s. Duplicate subitems may be created; use only for testing.",
        scope,
    )

    async def _forced_find(payload, lead_item_id, settings=None):
        target = str(lead_item_id)
        if force_item_id in (FORCE_ALL_SENTINEL, target):
            logger.warning(
                "BYPASS: returning None for existing-subitem lookup (lead=%s title=%r date=%s) "
                "to force a fresh create_subitem attempt.",
                target,
                payload.meeting_title,
                payload.meeting_date.isoformat(),
            )
            return None
        return await real_find(payload, lead_item_id, settings=settings)

    # meeting_subitem_already_exists (used by batch.py) calls this module-level name.
    meeting_module.find_existing_meeting_subitem_id = _forced_find
    # pipeline.py imported the name into its own namespace, so patch it there too.
    pipeline_module.find_existing_meeting_subitem_id = _forced_find


async def _run(days: int, *, force: bool, force_item_id: str) -> int:
    # Imported after load_dotenv so settings pick up the live .env values.
    from crm_integration.batch import process_recent_notetaker_meetings
    from crm_integration.monday_fetcher import ISR_TZ

    logger = logging.getLogger("run_5_day_backfill")

    if force:
        _install_idempotency_bypass(force_item_id)

    now = datetime.now(ISR_TZ)
    since = now - timedelta(days=days)
    hours = days * 24

    logger.info(
        "Starting %d-day Notetaker backfill: now=%s since=%s (%s)",
        days,
        now.isoformat(),
        since.isoformat(),
        ISR_TZ.key,
    )

    summary = await process_recent_notetaker_meetings(hours=hours)

    logger.info(
        "Backfill complete: fetched=%s processed=%s skipped=%s errors=%s",
        summary.get("fetched"),
        summary.get("processed_count"),
        summary.get("skipped_count"),
        summary.get("error_count"),
    )
    print("\n===== BACKFILL SUMMARY =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    return 1 if summary.get("error_count") else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Notetaker CRM batch for the past N days against live Monday boards.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Number of days to look back (default: {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Bypass the idempotency guard so already-backfilled meetings are re-created "
            "(for testing _verify_subitem_nested). WRITES DUPLICATES."
        ),
    )
    parser.add_argument(
        "--force-item-id",
        default=DEFAULT_FORCE_ITEM_ID,
        help=(
            "Lead item_id to scope the --force bypass to "
            f"(default: {DEFAULT_FORCE_ITEM_ID}). Use 'all' to bypass every meeting."
        ),
    )
    args = parser.parse_args()

    if args.days <= 0:
        parser.error("--days must be a positive integer")

    load_dotenv()
    _configure_logging()

    exit_code = asyncio.run(
        _run(args.days, force=args.force, force_item_id=args.force_item_id)
    )
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
