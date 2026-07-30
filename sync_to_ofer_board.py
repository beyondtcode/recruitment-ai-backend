"""
One-off script: fetch unseen CV emails from the last 2 days (yesterday + today),
process through the AI pipeline, upsert to the Main Hub, and create items on
Ofer's Monday board (4 columns only).
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from imap_tools import AND, MailBox
from pydantic import ValidationError

from services.ai_service import analyze_cv_with_claude
from services.monday_service import (
    CREATE_ITEM_MUTATION,
    normalize_email,
    normalize_phone,
    post_graphql,
    upload_file_to_item,
    upsert_candidate_item,
)
from utils.file_parser import extract_text_from_file

_BACKEND_ROOT = Path(__file__).resolve().parent
TEMP_DIR = _BACKEND_ROOT / "temp_ofer_sync"
ALLOWED_EXTENSIONS = {".pdf", ".docx"}

OFER_BOARD_ID = "5098604137"
OFER_GROUP_ID = "topics"
OFER_EMAIL_COLUMN_ID = "email_mm438sbe"
OFER_PHONE_COLUMN_ID = "phone_mm43s4mh"
OFER_FILE_COLUMN_ID = "file_mm43j6y2"


def fetch_unseen_cv_attachments() -> list[str]:
    """
    Connect to IMAP and download PDF/DOCX attachments from UNSEEN INBOX messages
    received since yesterday (covers yesterday and today). Messages are marked
    as read during fetch so they are not processed again.
    """
    host = os.getenv("EMAIL_HOST")
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")

    if not host or not user or not password:
        raise ValueError("EMAIL_HOST, EMAIL_USER, and EMAIL_PASSWORD must be set in .env")

    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    today = datetime.date.today()
    print(
        f"[1/4] Connecting to IMAP and fetching UNSEEN emails "
        f"from {yesterday.isoformat()} through {today.isoformat()}..."
    )

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    message_count = 0

    with MailBox(host).login(user, password, initial_folder="INBOX") as mailbox:
        for msg in mailbox.fetch(
            AND(seen=False, date_gte=yesterday),
            mark_seen=True,
        ):
            message_count += 1
            attachment_count = 0
            for attachment in msg.attachments:
                filename = attachment.filename
                if not filename:
                    continue

                suffix = Path(filename).suffix.lower()
                if suffix not in ALLOWED_EXTENSIONS:
                    continue

                safe_name = f"{msg.uid}_{Path(filename).name}"
                dest_path = TEMP_DIR / safe_name
                dest_path.write_bytes(attachment.payload)
                saved_paths.append(str(dest_path.resolve()))
                attachment_count += 1
                print(f"  Saved attachment from UID {msg.uid}: {dest_path.name}")

            if attachment_count == 0:
                print(f"  UID {msg.uid}: no PDF/DOCX attachments (marked as read)")

    print(
        f"[1/4] Done — scanned {message_count} unseen message(s), "
        f"saved {len(saved_paths)} CV file(s) to {TEMP_DIR}/"
    )
    return saved_paths


def _build_ofer_column_values(candidate) -> dict:
    """Build Monday column_values for Ofer's board (email + phone only)."""
    column_values: dict = {}

    email = (candidate.email or "").strip()
    if email:
        normalized = normalize_email(email)
        column_values[OFER_EMAIL_COLUMN_ID] = {
            "email": normalized,
            "text": normalized,
        }

    phone = (candidate.phone or "").strip()
    if phone:
        column_values[OFER_PHONE_COLUMN_ID] = {
            "phone": normalize_phone(phone),
            "countryShortName": "IL",
        }

    return column_values


async def create_ofer_board_item(candidate, cv_file_path: str) -> str:
    """Create a new item on Ofer's board with name, email, phone, and CV file."""
    column_values = _build_ofer_column_values(candidate)
    item_name = (candidate.name or "").strip() or Path(cv_file_path).stem

    print(f"  Creating Ofer board item: {item_name!r} (board {OFER_BOARD_ID}, group {OFER_GROUP_ID})")

    body = await post_graphql(
        CREATE_ITEM_MUTATION,
        {
            "boardId": OFER_BOARD_ID,
            "groupId": OFER_GROUP_ID,
            "itemName": item_name,
            "columnValues": json.dumps(column_values),
        },
        column_ids=list(column_values.keys()),
    )

    item_id = str(body["data"]["create_item"]["id"])
    print(f"  Ofer board item created: {item_id}")

    print(f"  Uploading CV to column {OFER_FILE_COLUMN_ID}...")
    await upload_file_to_item(item_id, cv_file_path, column_id=OFER_FILE_COLUMN_ID)
    print(f"  CV file attached to Ofer board item {item_id}")

    return item_id


async def process_cv_file(file_path: Path) -> None:
    """Parse, extract, upsert to Main Hub, and create on Ofer's board."""
    print(f"\n--- Processing: {file_path.name} ---")

    file_bytes = file_path.read_bytes()
    print("  [2/4] Extracting text from file...")
    cv_text = extract_text_from_file(file_bytes, file_path.name)
    print(f"  Extracted {len(cv_text)} characters of text")

    print("  [3/4] Sending to Claude for candidate extraction...")
    candidate = await analyze_cv_with_claude(cv_text)
    print(f"  Candidate: {candidate.name!r} | {candidate.email!r} | {candidate.phone!r}")

    print("  Upserting to Main Hub...")
    try:
        hub_id, hub_created = await upsert_candidate_item(
            candidate,
            cv_file_path=str(file_path),
            raw_cv_text=cv_text,
        )
        hub_action = "Created" if hub_created else "Updated"
        print(f"  Main Hub {hub_action.lower()}: item {hub_id}")
    except Exception as exc:
        print(f"  WARNING — Main Hub upsert failed (continuing to Ofer board): {exc}")

    print("  [4/4] Inserting into Ofer's board...")
    ofer_id = await create_ofer_board_item(candidate, str(file_path))
    print(f"  Ofer board insert complete: item {ofer_id}")


async def run() -> None:
    load_dotenv(_BACKEND_ROOT / ".env")

    saved_paths = fetch_unseen_cv_attachments()
    if not saved_paths:
        print("\nNo PDF/DOCX CV attachments found in unseen emails from the last 2 days. Nothing to do.")
        return

    print(f"\nStarting pipeline for {len(saved_paths)} file(s)...")

    succeeded = 0
    failed = 0

    for path_str in saved_paths:
        file_path = Path(path_str)
        try:
            await process_cv_file(file_path)
            succeeded += 1
        except ValidationError as exc:
            failed += 1
            print(f"  ERROR — validation failed for {file_path.name}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"  ERROR — failed to process {file_path.name}: {exc}")
        finally:
            if file_path.exists():
                file_path.unlink()
                print(f"  Cleaned up temp file: {file_path.name}")

    print(
        f"\n=== Sync complete ===\n"
        f"  Succeeded: {succeeded}\n"
        f"  Failed:    {failed}\n"
        f"  Total:     {len(saved_paths)}"
    )


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
