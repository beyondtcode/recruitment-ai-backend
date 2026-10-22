"""Google Sheets access for the lead agent.

Two backends:
  - Apps Script web app (preferred, no Google Cloud needed): see
    scripts/leads_sheet_webapp.gs; set LEADS_SHEET_WEBAPP_URL + LEADS_SHEET_WEBAPP_TOKEN.
  - Google service account (gspread): GOOGLE_SERVICE_ACCOUNT_FILE.

Env vars:
  GOOGLE_SERVICE_ACCOUNT_FILE — path to the service-account JSON key
                                (in Docker: /app/data/google-sa.json)
  LEADS_SHEET_ID              — spreadsheet id (defaults to the leads sheet)
  LEADS_JOBS_TAB              — jobs tab name (default "Sheet1")

The sheet is read and written by header name, so columns can be reordered or
added by hand without breaking the agent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_SHEET_ID = "128NxMS1CpBCkfwEkQJEYns1aXANy-FSo9_vyjquyXTk"

TAB_COMPANIES = "Companies"
TAB_ARCHIVE = "Archive"
TAB_BLOCKED = "Blocked Companies"
TAB_DO_NOT_CONTACT = "Do Not Contact"
TAB_WEEKLY = "Weekly"

JOB_HEADERS: list[str] = [
    "Company Name",
    "Job Title",
    "Category",
    "Outreach Phrasing",
    "Job Summary",
    "Job Link",
    "Publish Date",
    "Found Date",
    "Last Checked",
    "Source",
    "Company LinkedIn",
]

COMPANY_HEADERS: list[str] = [
    "Company Name",
    "Company Size",
    "Company Type",
    "Target",
    "Open Jobs",
    "Latest Job",
    "Latest Job Link",
    "Category",
    "Outreach Phrasing",
    "Contact Name",
    "Contact Title",
    "Contact LinkedIn",
    "Alt Contact Name",
    "Alt Contact Title",
    "Alt Contact LinkedIn",
    "Outreach Status",
    "Status Updated",
    "Last Job Found",
    "Company LinkedIn",
    "Last Enriched",
]

ARCHIVE_HEADERS: list[str] = JOB_HEADERS + ["Closed Date", "Outreach Status"]
BLOCKED_HEADERS: list[str] = ["Company", "Name Variants"]
DO_NOT_CONTACT_HEADERS: list[str] = ["Company Name", "Reason"]
WEEKLY_HEADERS: list[str] = [
    "Company Name",
    "Company Size",
    "Latest Job",
    "Latest Job Link",
    "Other Jobs",
    "Outreach Phrasing",
    "Contact",
    "Contact LinkedIn",
    "Alt Contact",
    "Alt Contact LinkedIn",
    "Previous Outreach",
]

OUTREACH_STATUSES: list[str] = [
    "לא נשלח",
    "בקשת חברות נשלחה",
    "הודעה ראשונה נשלחה",
    "פולואפ נשלח",
    "ענה: חיובי",
    "ענה: שלילי",
    "לא רלוונטי",
]


@dataclass
class Table:
    headers: list[str]
    rows: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_values(cls, values: list[list[str]]) -> "Table":
        if not values:
            return cls(headers=[])
        headers = [str(h).strip() for h in values[0]]
        rows = []
        for raw in values[1:]:
            if not any(str(c).strip() for c in raw):
                continue
            rows.append({h: (str(raw[i]) if i < len(raw) else "") for i, h in enumerate(headers) if h})
        return cls(headers=headers, rows=rows)

    def ensure_headers(self, wanted: list[str]) -> None:
        for h in wanted:
            if h not in self.headers:
                self.headers.append(h)

    def to_values(self) -> list[list[str]]:
        return [list(self.headers)] + [[r.get(h, "") for h in self.headers] for r in self.rows]


class SheetStore(Protocol):
    url: str

    def read(self, tab: str) -> Table | None: ...
    def write(self, tab: str, table: Table, *, reset_colors: bool = False) -> None: ...
    def append(self, tab: str, table: Table, rows: list[dict[str, str]]) -> None: ...
    def highlight_rows(self, tab: str, row_numbers: list[int], rgb: tuple[float, float, float]) -> None: ...
    def set_dropdown(self, tab: str, column: str, values: list[str]) -> None: ...


class MemoryStore:
    """In-memory store for tests and dry runs."""

    def __init__(self, tabs: dict[str, list[list[str]]] | None = None) -> None:
        self.tabs: dict[str, list[list[str]]] = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}
        self.highlights: dict[str, list[int]] = {}
        self.dropdowns: dict[str, tuple[str, list[str]]] = {}
        self.url = "memory://sheet"

    def read(self, tab: str) -> Table | None:
        if tab not in self.tabs:
            return None
        return Table.from_values(self.tabs[tab])

    def write(self, tab: str, table: Table, *, reset_colors: bool = False) -> None:
        self.tabs[tab] = table.to_values()
        if reset_colors:
            self.highlights.pop(tab, None)

    def append(self, tab: str, table: Table, rows: list[dict[str, str]]) -> None:
        if tab not in self.tabs or not self.tabs[tab]:
            self.tabs[tab] = [list(table.headers)]
        else:
            self.tabs[tab][0] = list(table.headers)
        self.tabs[tab].extend([[r.get(h, "") for h in table.headers] for r in rows])

    def highlight_rows(self, tab: str, row_numbers: list[int], rgb: tuple[float, float, float]) -> None:
        self.highlights[tab] = list(row_numbers)

    def set_dropdown(self, tab: str, column: str, values: list[str]) -> None:
        self.dropdowns[tab] = (column, list(values))


def _col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


class GoogleSheetStore:
    def __init__(self, sheet_id: str, credentials_file: str) -> None:
        import gspread

        self._gc = gspread.service_account(filename=credentials_file)
        self._sh = self._gc.open_by_key(sheet_id)
        self.url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    def _ws(self, tab: str, *, create: bool = False, cols: int = 26):
        import gspread

        try:
            return self._sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            if not create:
                return None
            logger.info("lead_sheet: creating tab %r", tab)
            return self._sh.add_worksheet(title=tab, rows=100, cols=max(cols, 10))

    def read(self, tab: str) -> Table | None:
        ws = self._ws(tab)
        if ws is None:
            return None
        return Table.from_values(ws.get_all_values())

    def write(self, tab: str, table: Table, *, reset_colors: bool = False) -> None:
        values = table.to_values()
        ws = self._ws(tab, create=True, cols=len(table.headers))
        needed_rows = max(len(values) + 20, 100)
        needed_cols = max(len(table.headers), 1)
        if ws.row_count < needed_rows or ws.col_count < needed_cols:
            ws.resize(rows=max(ws.row_count, needed_rows), cols=max(ws.col_count, needed_cols))
        ws.batch_clear([f"A1:{_col_letter(ws.col_count - 1)}{ws.row_count}"])
        ws.update(range_name="A1", values=values, value_input_option="RAW")
        if reset_colors:
            ws.format(
                f"A2:{_col_letter(ws.col_count - 1)}{ws.row_count}",
                {"backgroundColor": {"red": 1, "green": 1, "blue": 1}, "textFormat": {"bold": False}},
            )
        ws.format(f"A1:{_col_letter(needed_cols - 1)}1", {"textFormat": {"bold": True}})

    def append(self, tab: str, table: Table, rows: list[dict[str, str]]) -> None:
        ws = self._ws(tab, create=True, cols=len(table.headers))
        existing = ws.row_values(1)
        if existing != table.headers:
            if ws.col_count < len(table.headers):
                ws.resize(cols=len(table.headers))
            ws.update(range_name="A1", values=[table.headers], value_input_option="RAW")
            ws.format(f"A1:{_col_letter(len(table.headers) - 1)}1", {"textFormat": {"bold": True}})
        if rows:
            ws.append_rows(
                [[r.get(h, "") for h in table.headers] for r in rows],
                value_input_option="RAW",
                table_range="A1",
            )

    def highlight_rows(self, tab: str, row_numbers: list[int], rgb: tuple[float, float, float]) -> None:
        ws = self._ws(tab)
        if ws is None or not row_numbers:
            return
        last = _col_letter(ws.col_count - 1)
        color = {"backgroundColor": {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}}
        ws.batch_format([{"range": f"A{n}:{last}{n}", "format": color} for n in row_numbers])

    def set_dropdown(self, tab: str, column: str, values: list[str]) -> None:
        ws = self._ws(tab)
        if ws is None:
            return
        headers = ws.row_values(1)
        if column not in headers:
            return
        col = headers.index(column)
        self._sh.batch_update(
            {
                "requests": [
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": ws.id,
                                "startRowIndex": 1,
                                "endRowIndex": ws.row_count,
                                "startColumnIndex": col,
                                "endColumnIndex": col + 1,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [{"userEnteredValue": v} for v in values],
                                },
                                "showCustomUi": True,
                                "strict": False,
                            },
                        }
                    }
                ]
            }
        )


class AppsScriptStore:
    """Sheet access through the Apps Script web app in scripts/leads_sheet_webapp.gs.

    Needs no Google Cloud project: the script runs as the sheet owner.
    """

    def __init__(
        self, webapp_url: str, token: str, sheet_id: str, *, timeout: float = 120.0, transport=None
    ) -> None:
        import httpx

        self._url = webapp_url
        self._token = token
        self._client = httpx.Client(timeout=timeout, follow_redirects=True, transport=transport)
        self.url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"

    def _call(self, action: str, **payload) -> dict:
        response = self._client.post(
            self._url, json={"token": self._token, "action": action, **payload}
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                "Apps Script web app did not return JSON — check the deployment "
                "('Who has access: Anyone') and the URL"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Apps Script web app error: {data['error']}")
        return data

    def ping(self) -> dict:
        return self._call("ping")

    def read(self, tab: str) -> Table | None:
        data = self._call("read", tab=tab)
        if not data.get("exists"):
            return None
        return Table.from_values(data.get("values") or [])

    def write(self, tab: str, table: Table, *, reset_colors: bool = False) -> None:
        self._call("write", tab=tab, values=table.to_values(), reset_colors=reset_colors)

    def append(self, tab: str, table: Table, rows: list[dict[str, str]]) -> None:
        self._call(
            "append",
            tab=tab,
            headers=list(table.headers),
            rows=[[r.get(h, "") for h in table.headers] for r in rows],
        )

    def highlight_rows(self, tab: str, row_numbers: list[int], rgb: tuple[float, float, float]) -> None:
        if not row_numbers:
            return
        color = "#" + "".join(f"{round(c * 255):02x}" for c in rgb)
        self._call("highlight", tab=tab, rows=list(row_numbers), color=color)

    def set_dropdown(self, tab: str, column: str, values: list[str]) -> None:
        self._call("dropdown", tab=tab, column=column, list=list(values))


def get_sheet_store() -> SheetStore:
    """Apps Script web app when LEADS_SHEET_WEBAPP_URL is set, else a service account."""
    sheet_id = (os.environ.get("LEADS_SHEET_ID") or DEFAULT_SHEET_ID).strip()
    webapp = (os.environ.get("LEADS_SHEET_WEBAPP_URL") or "").strip()
    if webapp:
        token = (os.environ.get("LEADS_SHEET_WEBAPP_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("LEADS_SHEET_WEBAPP_TOKEN is not set")
        return AppsScriptStore(webapp, token, sheet_id)
    creds = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "").strip()
    if not creds:
        raise RuntimeError("Set LEADS_SHEET_WEBAPP_URL (Apps Script) or GOOGLE_SERVICE_ACCOUNT_FILE")
    if not os.path.exists(creds):
        raise RuntimeError(f"Service account file not found: {creds}")
    return GoogleSheetStore(sheet_id, creds)


def jobs_tab() -> str:
    return (os.environ.get("LEADS_JOBS_TAB") or "Sheet1").strip()
