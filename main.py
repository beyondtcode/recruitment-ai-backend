"""CV ingestion: fetch from email, extract with Claude, push to Monday.com."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from services.cv_pipeline import process_cv_file
from services.email_service import fetch_new_cv_attachments

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_pipeline() -> None:
    saved_paths = fetch_new_cv_attachments()

    if not saved_paths:
        logger.info("No new CV attachments found in inbox.")
        return

    logger.info("Found %d CV file(s) to process.", len(saved_paths))

    for path_str in saved_paths:
        file_path = Path(path_str)
        logger.info("Processing: %s", file_path.name)
        try:
            await process_cv_file(file_path)
        except ValidationError as exc:
            logger.error("Validation failed for %s: %s", file_path.name, exc, exc_info=True)
        except Exception as exc:
            logger.error("Failed to process %s: %s", file_path.name, exc, exc_info=True)


def main() -> None:
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Pipeline interrupted.")
        sys.exit(130)
