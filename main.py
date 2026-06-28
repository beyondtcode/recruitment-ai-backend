"""CV ingestion: fetch from email, extract with Claude, push to Monday.com."""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from services.email_batch import process_email_cv_batch

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_pipeline() -> None:
    summary = await process_email_cv_batch(lookback_days=0)

    if summary.get("status") == "skipped":
        logger.info("Email CV pipeline skipped: %s", summary.get("reason"))
        return

    logger.info(
        "Email CV pipeline finished: attachments=%d created=%d updated=%d skipped=%d errors=%d",
        summary.get("attachment_count", 0),
        summary.get("created_count", 0),
        summary.get("updated_count", 0),
        summary.get("skipped_count", 0),
        summary.get("error_count", 0),
    )


def main() -> None:
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted.")
        sys.exit(130)
