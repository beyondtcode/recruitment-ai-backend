"""Web search for company / contact enrichment.

Providers (first configured wins; the second is used as a fallback when the
first fails, e.g. its monthly quota is used up):
  TAVILY_API_KEY — Tavily (free plan: 1,000 searches / month, no card)
  SERPER_API_KEY — Serper.dev (2,500 one-time free searches)

Each run gets a budget (LEADS_MAX_SEARCHES_PER_RUN, default 60) so 15 runs a
month stay under the free quota.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"


@dataclass(frozen=True)
class SearchResult:
    title: str
    link: str
    snippet: str


class SearchClient(Protocol):
    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]: ...


class BudgetExceeded(RuntimeError):
    pass


class TavilyClient:
    name = "tavily"

    def __init__(self, api_key: str, *, timeout: float = 25.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        body: dict = {"query": query, "max_results": min(num, 20), "search_depth": "basic"}
        if domain:
            body["include_domains"] = [domain]
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                TAVILY_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=" ".join((item.get("title") or "").split()),
                link=(item.get("url") or "").strip(),
                snippet=" ".join((item.get("content") or "").split())[:400],
            )
            for item in data.get("results") or []
            if (item.get("url") or "").strip()
        ]


class SerperClient:
    name = "serper"

    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        q = f"site:{domain} {query}" if domain else query
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                SERPER_URL,
                headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"q": q, "gl": "il", "num": num},
            )
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=" ".join((item.get("title") or "").split()),
                link=(item.get("link") or "").strip(),
                snippet=" ".join((item.get("snippet") or "").split()),
            )
            for item in data.get("organic") or []
            if (item.get("link") or "").strip()
        ]


class BudgetedSearch:
    """Wraps providers with a per-run cap and provider fallback; counts usage."""

    def __init__(self, providers: list, budget: int) -> None:
        self._providers = providers
        self.budget = budget
        self.used = 0
        self.by_provider: dict[str, int] = {}

    @property
    def remaining(self) -> int:
        return max(self.budget - self.used, 0)

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        if self.used >= self.budget:
            raise BudgetExceeded("search budget for this run is used up")
        last_exc: Exception | None = None
        for provider in self._providers:
            self.used += 1
            self.by_provider[provider.name] = self.by_provider.get(provider.name, 0) + 1
            try:
                return await provider.search(query, num=num, domain=domain)
            except Exception as exc:
                last_exc = exc
                logger.warning("search: %s failed (%s); trying fallback", provider.name, exc)
                if self.used >= self.budget:
                    break
        raise RuntimeError(f"all search providers failed: {last_exc}")


def get_search_client() -> BudgetedSearch | None:
    providers: list = []
    tavily = (os.environ.get("TAVILY_API_KEY") or "").strip()
    serper = (os.environ.get("SERPER_API_KEY") or "").strip()
    if tavily:
        providers.append(TavilyClient(tavily))
    if serper:
        providers.append(SerperClient(serper))
    if not providers:
        return None
    try:
        budget = int(os.environ.get("LEADS_MAX_SEARCHES_PER_RUN") or 60)
    except ValueError:
        budget = 60
    return BudgetedSearch(providers, budget)


def format_results(results: list[SearchResult], limit: int = 8) -> str:
    lines = []
    for i, r in enumerate(results[:limit], 1):
        lines.append(f"[{i}] {r.title}\n    {r.link}\n    {r.snippet}")
    return "\n".join(lines) or "(no results)"
