"""Fetch unseen CV attachments from the configured IMAP inbox."""

from __future__ import annotations

import datetime
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import AND, MailBox

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
TEMP_CV_DIR = _BACKEND_ROOT / "temp_received_cvs"
ALLOWED_EXTENSIONS = {".pdf", ".docx"}


def fetch_new_cv_attachments() -> list[str]:
    """
    Connect to IMAP, download PDF/DOCX attachments from unseen INBOX messages
    received today or later, mark those messages as seen, and return local file paths.
    """
    load_dotenv(_BACKEND_ROOT / ".env")

    host = os.getenv("EMAIL_HOST")
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")

    if not host or not user or not password:
        raise ValueError("EMAIL_HOST, EMAIL_USER, and EMAIL_PASSWORD must be set in .env")

    TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []

    with MailBox(host).login(user, password, initial_folder="INBOX") as mailbox:
        for msg in mailbox.fetch(AND(seen=False, date=datetime.date.today())):
            for attachment in msg.attachments:
                filename = attachment.filename
                if not filename:
                    continue

                suffix = Path(filename).suffix.lower()
                if suffix not in ALLOWED_EXTENSIONS:
                    continue

                safe_name = f"{msg.uid}_{Path(filename).name}"
                dest_path = TEMP_CV_DIR / safe_name
                dest_path.write_bytes(attachment.payload)
                saved_paths.append(str(dest_path.resolve()))
                logger.info("Saved attachment from UID %s: %s", msg.uid, dest_path.name)

            mailbox.flag(msg.uid, "\\Seen", True)

    return saved_paths
