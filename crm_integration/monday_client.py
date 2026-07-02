from __future__ import annotations

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
