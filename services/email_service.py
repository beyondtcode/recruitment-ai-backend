"""Fetch CV attachments from the configured IMAP inbox."""

from __future__ import annotations

import datetime
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import AND, MailBox

from utils.file_parser import validate_cv_attachment_bytes

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
TEMP_CV_DIR = _BACKEND_ROOT / "temp_received_cvs"
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MIN_ATTACHMENT_BYTES = 2048

_DENYLISTED_FILENAME_RE = re.compile(
    r"(image\d*|logo|signature|banner|spacer|pixel|icon|footer|header)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CvEmailAttachment:
    path: str
    message_id: str
    filename: str
    sha256: str
    size_bytes: int


def _message_id_for(msg) -> str:
    headers = getattr(msg, "headers", None) or {}
    for key in ("message-id", "Message-ID", "Message-Id"):
        value = headers.get(key)
        if value:
            return str(value).strip()
    date_part = msg.date.date().isoformat() if msg.date else "unknown-date"
    return f"uid-{msg.uid}-{date_part}"


def _attachment_reject_reason(filename: str, payload: bytes) -> str | None:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return "bad_extension"

    if _DENYLISTED_FILENAME_RE.search(Path(filename).stem):
        return "denylisted_name"

    size = len(payload)
    if size < MIN_ATTACHMENT_BYTES:
        return "too_small"

    if validate_cv_attachment_bytes(filename, payload) is None:
        return "bad_magic"

    return None


def _imap_criteria(lookback_days: int):
    today = datetime.date.today()
    if lookback_days <= 0:
        return AND(date=today)
    since = today - datetime.timedelta(days=lookback_days)
    return AND(date_gte=since)


def fetch_cv_attachments(*, lookback_days: int = 1) -> list[CvEmailAttachment]:
    """
    Connect to IMAP and download PDF/DOCX attachments from all INBOX messages
    in the lookback window (read and unread). Does not mutate \\Seen flags.
    """
    load_dotenv(_BACKEND_ROOT / ".env")

    host = os.getenv("EMAIL_HOST")
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")

    if not host or not user or not password:
        raise ValueError("EMAIL_HOST, EMAIL_USER, and EMAIL_PASSWORD must be set in .env")

    TEMP_CV_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[CvEmailAttachment] = []
    criteria = _imap_criteria(lookback_days)

    with MailBox(host).login(user, password, initial_folder="INBOX") as mailbox:
        for msg in mailbox.fetch(criteria):
            message_id = _message_id_for(msg)
            id_prefix = hashlib.sha256(message_id.encode()).hexdigest()[:12]

            for attachment in msg.attachments:
                filename = attachment.filename
                if not filename:
                    continue

                payload = attachment.payload
                reject_reason = _attachment_reject_reason(filename, payload)
                if reject_reason:
                    logger.info(
                        "Skipped attachment from UID %s (%r): %s",
                        msg.uid,
                        filename,
                        reject_reason,
                    )
                    continue

                digest = hashlib.sha256(payload).hexdigest()
                safe_name = f"{id_prefix}_{msg.uid}_{Path(filename).name}"
                dest_path = TEMP_CV_DIR / safe_name
                dest_path.write_bytes(payload)

                saved.append(
                    CvEmailAttachment(
                        path=str(dest_path.resolve()),
                        message_id=message_id,
                        filename=Path(filename).name,
                        sha256=digest,
                        size_bytes=len(payload),
                    )
                )
                logger.info(
                    "Saved attachment from UID %s: %s (%d bytes)",
                    msg.uid,
                    dest_path.name,
                    len(payload),
                )

    return saved
