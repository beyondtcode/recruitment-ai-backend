from __future__ import annotations

import json
from typing import Any

from services.monday_service import post_graphql

ITEMS_PAGE_BY_COLUMN_VALUES_QUERY = """
query ($boardId: ID!, $limit: Int!, $columns: [ItemsPageByColumnValuesQuery!]!) {
  items_page_by_column_values(board_id: $boardId, limit: $limit, columns: $columns) {
    items {
      id
      name
    }
  }
}
"""

ITEMS_PAGE_BY_COLUMN_VALUES_WITH_COLUMNS_QUERY = """
query ($boardId: ID!, $limit: Int!, $columns: [ItemsPageByColumnValuesQuery!]!, $columnIds: [String!]!) {
  items_page_by_column_values(board_id: $boardId, limit: $limit, columns: $columns) {
    items {
      id
      name
      column_values(ids: $columnIds) {
        id
        text
        value
      }
    }
  }
}
"""

ITEMS_PAGE_WITH_COLUMNS_QUERY = """
query ($boardId: ID!, $limit: Int!, $columnIds: [String!]!, $queryParams: ItemsQuery!) {
  boards(ids: [$boardId]) {
    items_page(limit: $limit, query_params: $queryParams) {
      items {
        id
        name
        column_values(ids: $columnIds) {
          id
          text
          value
          ... on BoardRelationValue {
            linked_item_ids
          }
        }
      }
    }
  }
}
"""

BOARD_ITEMS_PAGE_CURSOR_QUERY = """
query ($boardId: ID!, $limit: Int!, $columnIds: [String!]!, $cursor: String) {
  boards(ids: [$boardId]) {
    items_page(limit: $limit, cursor: $cursor) {
      cursor
      items {
        id
        name
        column_values(ids: $columnIds) {
          id
          text
          value
          ... on BoardRelationValue {
            linked_item_ids
          }
        }
      }
    }
  }
}
"""

ITEMS_BY_IDS_QUERY = """
query ($ids: [ID!]!, $columnIds: [String!]!) {
  items(ids: $ids) {
    id
    name
    column_values(ids: $columnIds) {
      id
      text
      value
      ... on BoardRelationValue {
        linked_item_ids
      }
    }
  }
}
"""

CREATE_ITEM_MUTATION = """
mutation ($boardId: ID!, $groupId: String!, $itemName: String!, $columnValues: JSON!) {
  create_item (
    board_id: $boardId,
    group_id: $groupId,
    item_name: $itemName,
    column_values: $columnValues,
    create_labels_if_missing: true
  ) {
    id
  }
}
"""

CREATE_SUBITEM_MUTATION = """
mutation ($parentItemId: ID!, $itemName: String!, $columnValues: JSON!) {
  create_subitem (
    parent_item_id: $parentItemId,
    item_name: $itemName,
    column_values: $columnValues,
    create_labels_if_missing: true
  ) {
    id
  }
}
"""

ITEM_SUBITEMS_WITH_COLUMNS_QUERY = """
query ($itemId: ID!, $columnIds: [String!]!) {
  items(ids: [$itemId]) {
    subitems {
      id
      name
      column_values(ids: $columnIds) {
        id
        text
        value
      }
    }
  }
}
"""

CHANGE_MULTIPLE_COLUMN_VALUES_MUTATION = """
mutation ($boardId: ID!, $itemId: ID!, $columnValues: JSON!) {
  change_multiple_column_values(
    board_id: $boardId,
    item_id: $itemId,
    column_values: $columnValues,
    create_labels_if_missing: true
  ) {
    id
  }
}
"""

DELETE_ITEM_MUTATION = """
mutation ($itemId: ID!) {
  delete_item(item_id: $itemId) {
    id
  }
}
"""

CREATE_DOC_MUTATION = """
mutation ($itemId: ID!, $columnId: String!) {
  create_doc(location: { board: { item_id: $itemId, column_id: $columnId } }) {
    id
    object_id
  }
}
"""

CREATE_DOC_BLOCK_MUTATION = """
mutation ($docId: ID!, $type: DocBlockContentType!, $content: JSON!, $afterBlockId: String) {
  create_doc_block(
    doc_id: $docId
    type: $type
    content: $content
    after_block_id: $afterBlockId
  ) {
    id
  }
}
"""

USERS_BY_EMAILS_QUERY = """
query ($emails: [String!]!) {
  users(emails: $emails) {
    id
    email
  }
}
"""

FIND_ITEMS_LIMIT = 25
BOARD_ITEMS_PAGE_SIZE = 100
DOC_BLOCKS_PAGE_LIMIT = 30

ITEMS_WITH_DOC_COLUMN_QUERY = """
query ($ids: [ID!]!, $columnIds: [String!]!) {
  items(ids: $ids) {
    column_values(ids: $columnIds) {
      id
      value
      ... on DocValue {
        file {
          doc {
            id
          }
        }
      }
    }
  }
}
"""

DOCS_BLOCKS_QUERY = """
query ($docId: ID!, $limit: Int!, $page: Int!) {
  docs(ids: [$docId]) {
    blocks(limit: $limit, page: $page) {
      id
      type
      content
      position
    }
  }
}
"""


async def fetch_doc_blocks(
    doc_id: str,
    *,
    limit: int = DOC_BLOCKS_PAGE_LIMIT,
    page: int = 1,
) -> list[dict[str, Any]]:
    """Fetch a single page of blocks from a Monday Workdoc."""
    body = await execute_graphql(
        DOCS_BLOCKS_QUERY,
        {"docId": int(doc_id), "limit": limit, "page": page},
    )
    docs = body.get("data", {}).get("docs") or []
    if not docs:
        return []
    return list(docs[0].get("blocks") or [])


async def fetch_all_board_items(
    board_id: str,
    column_ids: list[str],
    *,
    page_size: int = BOARD_ITEMS_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch all items from a Monday board, paging with cursor until exhausted."""
    all_items: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        variables: dict[str, Any] = {
            "boardId": board_id,
            "limit": page_size,
            "columnIds": column_ids,
        }
        if cursor:
            variables["cursor"] = cursor

        body = await execute_graphql(
            BOARD_ITEMS_PAGE_CURSOR_QUERY,
            variables,
            column_ids=column_ids,
        )
        boards = body.get("data", {}).get("boards") or []
        if not boards:
            break

        items_page = boards[0].get("items_page") or {}
        items = list(items_page.get("items") or [])
        all_items.extend(items)

        next_cursor = items_page.get("cursor")
        if not items or not next_cursor or next_cursor in seen_cursors:
            break
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    return all_items


ITEMS_BY_IDS_BATCH_SIZE = 25


async def fetch_items_by_ids(
    item_ids: list[str],
    column_ids: list[str],
    *,
    batch_size: int = ITEMS_BY_IDS_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """Fetch Monday items by ID with board-relation linked_item_ids support."""
    if not item_ids:
        return []

    all_items: list[dict[str, Any]] = []
    for offset in range(0, len(item_ids), batch_size):
        batch = item_ids[offset : offset + batch_size]
        body = await execute_graphql(
            ITEMS_BY_IDS_QUERY,
            {"ids": batch, "columnIds": column_ids},
            column_ids=column_ids,
        )
        all_items.extend(body.get("data", {}).get("items") or [])
    return all_items


async def fetch_item_doc_id(item_id: str, doc_column_id: str) -> str | None:
    """Return the Workdoc ID attached to an item's doc column, if any."""
    body = await execute_graphql(
        ITEMS_WITH_DOC_COLUMN_QUERY,
        {"ids": [item_id], "columnIds": [doc_column_id]},
        column_ids=[doc_column_id],
    )
    items = body.get("data", {}).get("items") or []
    if not items:
        return None

    for column in items[0].get("column_values") or []:
        if str(column.get("id")) != doc_column_id:
            continue
        file_data = column.get("file") or {}
        doc = file_data.get("doc") or {}
        doc_id = doc.get("id")
        if doc_id is not None:
            return str(doc_id)

        value = column.get("value")
        if not value:
            return None
        try:
            parsed = json.loads(value) if isinstance(value, str) else value
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        for file_entry in parsed.get("files") or []:
            if not isinstance(file_entry, dict):
                continue
            if file_entry.get("fileType") == "MONDAY_DOC":
                object_id = file_entry.get("objectId")
                if object_id is not None:
                    return str(object_id)
    return None


async def fetch_all_doc_blocks(
    doc_id: str,
    *,
    page_limit: int = DOC_BLOCKS_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    """Fetch all blocks from a Monday Workdoc, paging until exhausted."""
    all_blocks: list[dict[str, Any]] = []
    page = 1
    while True:
        blocks = await fetch_doc_blocks(doc_id, limit=page_limit, page=page)
        if not blocks:
            break
        all_blocks.extend(blocks)
        if len(blocks) < page_limit:
            break
        page += 1
    return all_blocks


async def delete_monday_item(item_id: str) -> str:
    """Delete a Monday item and return its deleted id."""
    body = await execute_graphql(DELETE_ITEM_MUTATION, {"itemId": int(item_id)})
    deleted_id = body.get("data", {}).get("delete_item", {}).get("id")
    if not deleted_id:
        raise RuntimeError(f"delete_item returned no id for item {item_id}")
    return str(deleted_id)


async def execute_graphql(
    query: str,
    variables: dict[str, Any],
    *,
    column_ids: list[str] | None = None,
    api_version: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    return await post_graphql(
        query,
        variables,
        column_ids=column_ids,
        api_version=api_version,
        api_key=api_key,
    )
