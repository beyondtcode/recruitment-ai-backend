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
