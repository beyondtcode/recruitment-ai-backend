"""Web search for company / contact enrichment (Serper.dev Google Search API).

Env vars:
  SERPER_API_KEY — required for company and contact enrichment.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    snippet: str


class SearchClient(Protocol):
    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]: ...


class SerperClient:
    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self.calls = 0

    async def search(self, query: str, *, num: int = 10) -> list[SearchResult]:
        self.calls += 1
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                SERPER_URL,
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"q": query, "gl": "il", "num": num},
            )
            response.raise_for_status()
            data = response.json()
        results = []
        for item in data.get("organic") or []:
            link = (item.get("link") or "").strip()
            if not link:
                continue
            results.append(
                SearchResult(
                    title=" ".join((item.get("title") or "").split()),
                    link=link,
                    snippet=" ".join((item.get("snippet") or "").split()),
                )
            )
        return results


def get_search_client() -> SearchClient | None:
    key = (os.environ.get("SERPER_API_KEY") or "").strip()
    if not key:
        return None
    return SerperClient(key)


def format_results(results: list[SearchResult], limit: int = 8) -> str:
    lines = []
    for i, r in enumerate(results[:limit], 1):
        lines.append(f"[{i}] {r.title}\n    {r.link}\n    {r.snippet}")
    return "\n".join(lines) or "(no results)"
