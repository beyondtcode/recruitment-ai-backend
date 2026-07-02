from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

from crm_integration.config import CrmSettings, MEETING_ANALYSIS_DOC_COLUMN_ID, get_crm_settings
from crm_integration.meeting import (
    BoardKind,
    extract_meeting_summary_intro,
    get_lead_analysis_doc_id,
    _meeting_target_board,
)
from crm_integration.monday_client import (
    CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
    CREATE_DOC_BLOCK_MUTATION,
    CREATE_DOC_MUTATION,
    execute_graphql,
)
from crm_integration.schemas import NodeTakerWebhookPayload

logger = logging.getLogger(__name__)

DocBlockType = Literal[
    "large_title",
    "medium_title",
    "small_title",
    "normal_text",
    "bulleted_list",
    "numbered_list",
]


@dataclass(frozen=True)
class DocBlockSpec:
    block_type: DocBlockType
    text: str


def build_meeting_details_markdown(payload: NodeTakerWebhookPayload) -> str:
    """Build full meeting details markdown for the Monday Workdoc."""
    lines = [
        f"# {payload.meeting_title.strip()}",
        "",
        f"**Date:** {payload.meeting_date.isoformat()}",
        "",
    ]
    if payload.participant_emails:
        lines += ["## Participants", ""]
        lines += [f"- {email}" for email in payload.participant_emails]
        lines.append("")
    if payload.meeting_summary.strip():
        lines += ["## Summary", "", payload.meeting_summary.strip(), ""]
    if payload.action_items.strip():
        lines += ["## Action Items", "", payload.action_items.strip()]
    return "\n".join(lines).strip()


def _split_paragraphs(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return paragraphs or ([text.strip()] if text.strip() else [])


def _parse_markdown_lines(text: str) -> list[DocBlockSpec]:
    """Parse markdown lines into doc block specs (headers, bullets, paragraphs)."""
    blocks: list[DocBlockSpec] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append(DocBlockSpec("small_title", stripped[4:].strip()))
        elif stripped.startswith("## "):
            blocks.append(DocBlockSpec("medium_title", stripped[3:].strip()))
        elif stripped.startswith("# "):
            blocks.append(DocBlockSpec("large_title", stripped[2:].strip()))
        elif stripped.startswith("- ") or stripped.startswith("* "):
            blocks.append(DocBlockSpec("bulleted_list", stripped[2:].strip()))
        elif re.match(r"^\d+\.\s+", stripped):
            blocks.append(DocBlockSpec("numbered_list", re.sub(r"^\d+\.\s+", "", stripped)))
        else:
            blocks.append(DocBlockSpec("normal_text", stripped))
    return blocks


def build_meeting_doc_blocks(payload: NodeTakerWebhookPayload) -> list[DocBlockSpec]:
    """Build ordered doc blocks for a meeting Workdoc."""
    blocks: list[DocBlockSpec] = []

    title = payload.meeting_title.strip()
    if title:
        blocks.append(DocBlockSpec("large_title", title))

    blocks.append(
        DocBlockSpec("normal_text", f"Date: {payload.meeting_date.isoformat()}")
    )

    if payload.participant_emails:
        blocks.append(DocBlockSpec("medium_title", "Participants"))
        for email in payload.participant_emails:
            blocks.append(DocBlockSpec("bulleted_list", str(email)))

    summary = payload.meeting_summary.strip()
    if summary:
        blocks.append(DocBlockSpec("medium_title", "Summary"))
        for paragraph in _split_paragraphs(summary):
            blocks.extend(_parse_markdown_lines(paragraph))

    action_items = payload.action_items.strip()
    if action_items:
        blocks.append(DocBlockSpec("medium_title", "Action Items"))
        blocks.extend(_parse_markdown_lines(action_items))

    return blocks


def _block_content_json(block: DocBlockSpec) -> str:
    base: dict[str, object] = {
        "alignment": "left",
        "direction": "ltr",
        "deltaFormat": [{"insert": block.text}],
    }
    if block.block_type in {"bulleted_list", "numbered_list"}:
        base["indentation"] = 1
    return json.dumps(base)


async def _insert_doc_blocks(doc_id: str, blocks: list[DocBlockSpec]) -> list[str]:
    """Insert blocks sequentially into a Workdoc. Returns created block IDs."""
    created_ids: list[str] = []
    after_block_id: str | None = None

    for index, block in enumerate(blocks):
        variables: dict[str, object] = {
            "docId": int(doc_id),
            "type": block.block_type,
            "content": _block_content_json(block),
        }
        if after_block_id is not None:
            variables["afterBlockId"] = after_block_id

        body = await execute_graphql(CREATE_DOC_BLOCK_MUTATION, variables)
        block_id = body.get("data", {}).get("create_doc_block", {}).get("id")
        if not block_id:
            raise RuntimeError(
                f"create_doc_block returned no id for block {index + 1}/{len(blocks)} "
                f"({block.block_type})"
            )

        after_block_id = str(block_id)
        created_ids.append(after_block_id)
        logger.debug(
            "Inserted doc block %d/%d (%s) id=%s",
            index + 1,
            len(blocks),
            block.block_type,
            after_block_id,
        )

    return created_ids


async def _append_action_items_to_summary_column(
    item_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
    *,
    board_kind: BoardKind = "customer",
) -> None:
    action_items = payload.action_items.strip()
    if not action_items:
        return

    overview = extract_meeting_summary_intro(payload.meeting_summary)
    combined = overview
    if combined:
        combined += "\n\n## Action Items\n\n"
    else:
        combined = "## Action Items\n\n"
    combined += action_items

    board_id, _ = _meeting_target_board(settings, board_kind)
    await execute_graphql(
        CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION,
        {
            "boardId": board_id,
            "itemId": item_id,
            "columnValues": json.dumps(
                {settings.monday_crm_meeting_summary_column_id: {"text": combined}}
            ),
        },
        column_ids=[settings.monday_crm_meeting_summary_column_id],
    )
    logger.info(
        "Appended action items to summary column for meeting item %s after doc failure",
        item_id,
    )


async def create_meeting_workdoc(
    item_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings | None = None,
    *,
    board_kind: BoardKind = "customer",
) -> tuple[str | None, bool, list[str]]:
    """
    Create or prepend to a meeting Workdoc.

    Customer path writes to the lead's rolling analysis column.
    Company path creates a Workdoc on the meeting item.

    Returns (doc_id, doc_created, warnings).
    """
    settings = settings or get_crm_settings()
    warnings: list[str] = []
    blocks = build_meeting_doc_blocks(payload)

    if not blocks:
        warnings.append("No meeting details to write to Workdoc")
        return None, False, warnings

    if board_kind == "company":
        return await _create_meeting_item_workdoc(
            item_id,
            payload,
            settings,
            blocks,
            warnings,
            board_kind=board_kind,
        )

    return await _upsert_lead_meeting_workdoc(item_id, blocks, settings, warnings)


async def _upsert_lead_meeting_workdoc(
    lead_item_id: str,
    blocks: list[DocBlockSpec],
    settings: CrmSettings,
    warnings: list[str],
) -> tuple[str | None, bool, list[str]]:
    doc_id = await get_lead_analysis_doc_id(lead_item_id, settings=settings)
    doc_created = False

    if not doc_id:
        try:
            doc_body = await execute_graphql(
                CREATE_DOC_MUTATION,
                {
                    "itemId": int(lead_item_id),
                    "columnId": MEETING_ANALYSIS_DOC_COLUMN_ID,
                },
            )
            doc_id = doc_body.get("data", {}).get("create_doc", {}).get("id")
            if not doc_id:
                raise RuntimeError("Monday create_doc did not return a doc id")
            doc_id = str(doc_id)
            doc_created = True
            logger.info(
                "Rolling Workdoc created with ID %s for lead item %s",
                doc_id,
                lead_item_id,
            )
        except Exception as exc:
            logger.warning(
                "Rolling Workdoc creation failed for lead item %s: %s",
                lead_item_id,
                exc,
            )
            warnings.append(f"Workdoc creation failed: {exc}")
            return None, False, warnings

    try:
        block_ids = await _insert_doc_blocks(doc_id, blocks)
        logger.info(
            "Meeting details prepended to rolling Workdoc %s (%d blocks)",
            doc_id,
            len(block_ids),
        )
        return doc_id, doc_created, warnings
    except Exception as exc:
        logger.warning(
            "Rolling Workdoc %s block insertion failed for lead item %s: %s",
            doc_id,
            lead_item_id,
            exc,
        )
        warnings.append(f"Workdoc block insertion failed: {exc}")
        return doc_id, doc_created, warnings


async def _create_meeting_item_workdoc(
    item_id: str,
    payload: NodeTakerWebhookPayload,
    settings: CrmSettings,
    blocks: list[DocBlockSpec],
    warnings: list[str],
    *,
    board_kind: BoardKind,
) -> tuple[str | None, bool, list[str]]:
    doc_id: str | None = None
    try:
        doc_body = await execute_graphql(
            CREATE_DOC_MUTATION,
            {
                "itemId": int(item_id),
                "columnId": settings.monday_crm_meeting_doc_column_id,
            },
        )
        doc_id = doc_body.get("data", {}).get("create_doc", {}).get("id")
        if not doc_id:
            raise RuntimeError("Monday create_doc did not return a doc id")
        doc_id = str(doc_id)
        logger.info("Workdoc created with ID %s for meeting item %s", doc_id, item_id)
    except Exception as exc:
        logger.warning(
            "Workdoc creation failed for meeting item %s; overview preserved in summary column: %s",
            item_id,
            exc,
        )
        warnings.append(f"Workdoc creation failed: {exc}")
        try:
            await _append_action_items_to_summary_column(
                item_id,
                payload,
                settings,
                board_kind=board_kind,
            )
        except Exception as append_exc:
            logger.error(
                "Failed to append action items to summary column for item %s: %s",
                item_id,
                append_exc,
            )
            warnings.append(f"Failed to append action items to summary column: {append_exc}")
        return None, False, warnings

    try:
        block_ids = await _insert_doc_blocks(doc_id, blocks)
        logger.info(
            "Meeting details written to Workdoc %s (%d blocks)",
            doc_id,
            len(block_ids),
        )
        return doc_id, True, warnings
    except Exception as exc:
        logger.warning(
            "Workdoc %s created but block insertion failed for meeting item %s: %s",
            doc_id,
            item_id,
            exc,
        )
        warnings.append(f"Workdoc block insertion failed: {exc}")
        try:
            await _append_action_items_to_summary_column(
                item_id,
                payload,
                settings,
                board_kind=board_kind,
            )
        except Exception as append_exc:
            logger.error(
                "Failed to append action items to summary column for item %s: %s",
                item_id,
                append_exc,
            )
            warnings.append(f"Failed to append action items to summary column: {append_exc}")
        return doc_id, True, warnings
