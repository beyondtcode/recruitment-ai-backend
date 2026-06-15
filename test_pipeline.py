"""Local end-to-end test for CV file extraction and Claude parsing."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from services.ai_service import analyze_cv_with_claude
from services.monday_service import upsert_candidate_item
from utils.file_parser import extract_text_from_file

TEST_CVS_DIR = Path(__file__).resolve().parent / "test_cvs"
SUPPORTED_SUFFIXES = {".pdf", ".docx"}

# ANSI colors for terminal JSON output
RESET = "\033[0m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
RED = "\033[31m"
DIM = "\033[2m"


def _use_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def colorize_json(json_text: str) -> str:
    if not _use_color():
        return json_text

    def colorize(match: re.Match[str]) -> str:
        token = match.group(0)
        if token.startswith('"') and token.endswith('":'):
            return f"{CYAN}{token}{RESET}"
        if token in ("true", "false"):
            return f"{YELLOW}{token}{RESET}"
        if token == "null":
            return f"{MAGENTA}{token}{RESET}"
        if token[0] in '"{[':
            return f"{GREEN}{token}{RESET}"
        if re.fullmatch(r"-?\d+(\.\d+)?", token):
            return f"{YELLOW}{token}{RESET}"
        return token

    pattern = r'"(?:\\.|[^"\\])*"\s*:|"(?:\\.|[^"\\])*"|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?'
    return re.sub(pattern, colorize, json_text)


async def process_file(file_path: Path) -> None:
    separator = "=" * 72
    print(f"\n{BOLD}{separator}{RESET}")
    print(f"{BOLD}Processing:{RESET} {file_path.name}")
    print(f"{BOLD}{separator}{RESET}\n")

    file_bytes = file_path.read_bytes()
    cv_text = extract_text_from_file(file_bytes, file_path.name)

    print(f"{DIM}Extracted {len(cv_text)} characters of text. Calling Claude...{RESET}\n")

    candidate = await analyze_cv_with_claude(cv_text)
    formatted = candidate.model_dump_json(indent=2)
    print(colorize_json(formatted))
    print()

    try:
        item_id, created = await upsert_candidate_item(
            candidate,
            cv_file_path=str(file_path.resolve()),
            raw_cv_text=cv_text,
        )
        action = "Created" if created else "Updated"
        print(f"{GREEN}{BOLD}SUCCESS: {action} Monday Item ID -> {item_id}{RESET}\n")
    except Exception as exc:
        print(
            f"\n{RED}ERROR creating Monday item for {file_path.name}:{RESET} {exc}\n",
            file=sys.stderr,
        )


async def main() -> None:
    TEST_CVS_DIR.mkdir(parents=True, exist_ok=True)

    files = sorted(
        p
        for p in TEST_CVS_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )

    if not files:
        print(f"No PDF or DOCX files found in {TEST_CVS_DIR}")
        print("Add sample CVs to that folder and run this script again.")
        return

    print(f"Found {len(files)} file(s) in {TEST_CVS_DIR}\n")

    for file_path in files:
        try:
            await process_file(file_path)
        except Exception as exc:
            print(f"\n{RED}ERROR processing {file_path.name}:{RESET} {exc}\n", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
