"""Web search for company / contact enrichment.

Order: free first, paid only when needed.
  1. DuckDuckGo (free, no key) — used first for every search. Disable with
     LEADS_FREE_SEARCH=0.
  2. TAVILY_API_KEY — Tavily (free plan: 1,000 searches / month, no card)
  3. SERPER_API_KEY — Serper.dev (2,500 one-time free searches)

A paid provider is called only when the free search fails (blocked / error) or
finds nothing. There is no per-run cap by default (LEADS_MAX_SEARCHES_PER_RUN,
0 = no cap); the monthly Tavily quota is watched instead (``tavily_usage`` /
``quota_alert``) and a provider whose quota is used up is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

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


class QuotaExhausted(RuntimeError):
    """The provider's plan quota is used up (not a temporary error)."""


# Tavily: 432 = plan limit reached, 433 = pay-as-you-go limit reached.
_QUOTA_STATUS = {432, 433}


class TavilyClient:
    name = "tavily"
    free = False

    def __init__(self, api_key: str, *, timeout: float = 25.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self.exhausted = False

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
            if response.status_code in _QUOTA_STATUS or (
                response.status_code == 429 and "limit" in response.text.lower() and "usage" in response.text.lower()
            ):
                self.exhausted = True
                raise QuotaExhausted(f"Tavily quota used up (HTTP {response.status_code})")
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
    free = False

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


DDG_URL = "https://html.duckduckgo.com/html/"
_DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}


class SearchBlocked(RuntimeError):
    pass


def _ddg_real_url(href: str) -> str:
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<url>."""
    href = (href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target)
    return href


def parse_ddg_html(html: str) -> list[SearchResult]:
    soup = BeautifulSoup(html or "", "html.parser")
    if soup.select_one(".anomaly-modal__modal, #anomaly-modal") or "anomaly" in (soup.title.string or "" if soup.title else "").lower():
        raise SearchBlocked("DuckDuckGo asked for a captcha")
    results = []
    for block in soup.select(".result"):
        link = block.select_one("a.result__a")
        if link is None:
            continue
        url = _ddg_real_url(link.get("href", ""))
        if not url.startswith("http") or "duckduckgo.com/y.js" in url:
            continue  # ads
        snippet = block.select_one(".result__snippet")
        results.append(
            SearchResult(
                title=" ".join(link.get_text(" ", strip=True).split()),
                link=url,
                snippet=" ".join(snippet.get_text(" ", strip=True).split()) if snippet else "",
            )
        )
    return results


class DuckDuckGoClient:
    """Free web search via DuckDuckGo's HTML endpoint (no key). Requests are
    spaced out; a captcha / error raises so the caller can fall back."""

    name = "duckduckgo"
    free = True
    PAUSE_SECONDS = 2.0

    def __init__(self, *, timeout: float = 20.0) -> None:
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self.blocked = False

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        if self.blocked:
            raise SearchBlocked("DuckDuckGo blocked earlier in this run")
        q = f"site:{domain} {query}" if domain else query
        async with self._lock:
            try:
                async with httpx.AsyncClient(
                    headers=_DDG_HEADERS, timeout=self._timeout, follow_redirects=True
                ) as client:
                    response = await client.post(DDG_URL, data={"q": q, "kl": "il-en"})
            finally:
                await asyncio.sleep(self.PAUSE_SECONDS)
        if response.status_code != 200:
            if response.status_code in (202, 403, 429):
                self.blocked = True
            raise SearchBlocked(f"DuckDuckGo HTTP {response.status_code}")
        try:
            return parse_ddg_html(response.text)[:num]
        except SearchBlocked:
            self.blocked = True
            raise


class BudgetedSearch:
    """Free providers first; paid providers only when free fails or finds nothing.

    ``used`` / ``budget`` count paid calls only.
    """

    def __init__(self, providers: list, budget: int) -> None:
        self._free = [p for p in providers if getattr(p, "free", False)]
        self._paid = [p for p in providers if not getattr(p, "free", False)]
        self.budget = budget
        self.used = 0
        self.by_provider: dict[str, int] = {}

    @property
    def remaining(self) -> int:
        if self._paid and all(getattr(p, "exhausted", False) for p in self._paid):
            return 0
        if not self.budget:
            return 10**9  # no per-run cap
        return max(self.budget - self.used, 0)

    @property
    def exhausted_providers(self) -> list[str]:
        return [p.name for p in self._paid if getattr(p, "exhausted", False)]

    @property
    def has_free(self) -> bool:
        return any(not getattr(p, "blocked", False) for p in self._free)

    def _count(self, name: str) -> None:
        self.by_provider[name] = self.by_provider.get(name, 0) + 1

    def free_only(self) -> "_FreeOnly":
        """A view that never calls paid providers (used once a step's paid share is spent)."""
        return _FreeOnly(self)

    async def search(
        self, query: str, *, num: int = 10, domain: str | None = None, allow_paid: bool = True,
        paid_cap: int | None = None,
    ) -> list[SearchResult]:
        last_exc: Exception | None = None
        for provider in self._free:
            if getattr(provider, "blocked", False):
                continue
            self._count(provider.name)
            try:
                results = await provider.search(query, num=num, domain=domain)
            except Exception as exc:
                last_exc = exc
                logger.info("search: %s failed (%s); trying the next provider", provider.name, exc)
                continue
            if results:
                return results
        # paid_cap: this step may only use paid calls while the run total is
        # below it (checked right before the paid call, so concurrent lookups
        # cannot overshoot it).
        if paid_cap is not None and self.used >= paid_cap:
            allow_paid = False
        if not self._paid or not allow_paid:
            if last_exc is not None:
                raise RuntimeError(f"free search failed: {last_exc}")
            return []
        if self.budget and self.used >= self.budget:
            raise BudgetExceeded("paid search budget for this run is used up")
        paid = [p for p in self._paid if not getattr(p, "exhausted", False)]
        if not paid:
            raise BudgetExceeded("paid search quota is used up")
        for provider in paid:
            self.used += 1
            self._count(provider.name)
            try:
                return await provider.search(query, num=num, domain=domain)
            except Exception as exc:
                last_exc = exc
                logger.warning("search: %s failed (%s); trying fallback", provider.name, exc)
                if self.budget and self.used >= self.budget:
                    break
        if all(getattr(p, "exhausted", False) for p in self._paid):
            raise BudgetExceeded("paid search quota is used up")
        raise RuntimeError(f"all search providers failed: {last_exc}")


    def limited(self, paid_cap: int) -> "_Limited":
        """A view that uses paid providers only while the run's paid total is below ``paid_cap``."""
        return _Limited(self, paid_cap)


class _Limited:
    def __init__(self, parent: BudgetedSearch, paid_cap: int) -> None:
        self._parent = parent
        self._cap = paid_cap

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        return await self._parent.search(query, num=num, domain=domain, paid_cap=self._cap)


class _FreeOnly:
    def __init__(self, parent: BudgetedSearch) -> None:
        self._parent = parent

    async def search(self, query: str, *, num: int = 10, domain: str | None = None) -> list[SearchResult]:
        return await self._parent.search(query, num=num, domain=domain, allow_paid=False)


def get_search_client() -> BudgetedSearch | None:
    providers: list = []
    if (os.environ.get("LEADS_FREE_SEARCH") or "1").strip() not in ("0", "false", "no"):
        providers.append(DuckDuckGoClient())
    tavily = (os.environ.get("TAVILY_API_KEY") or "").strip()
    serper = (os.environ.get("SERPER_API_KEY") or "").strip()
    if tavily:
        providers.append(TavilyClient(tavily))
    if serper:
        providers.append(SerperClient(serper))
    if not providers:
        return None
    try:
        budget = int(os.environ.get("LEADS_MAX_SEARCHES_PER_RUN") or 0)
    except ValueError:
        budget = 0
    return BudgetedSearch(providers, budget)


TAVILY_USAGE_URL = "https://api.tavily.com/usage"


async def tavily_usage(api_key: str) -> tuple[int, int] | None:
    """(used, limit) of the Tavily plan this billing cycle, or None if unknown."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(TAVILY_USAGE_URL, headers={"Authorization": f"Bearer {api_key}"})
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.info("search: could not read Tavily usage", exc_info=True)
        return None
    for section, used_key, limit_key in (("account", "plan_usage", "plan_limit"), ("key", "usage", "limit")):
        part = data.get(section) or {}
        used, limit = part.get(used_key), part.get(limit_key)
        if isinstance(used, (int, float)) and isinstance(limit, (int, float)) and limit > 0:
            return int(used), int(limit)
    return None


def quota_alert(used: int | None, limit: int | None, run_used: int, exhausted: bool) -> str:
    """Hebrew warning for the Slack summary, or '' when the quota is fine.

    Warns when the quota ran out during the run, when 80% of it is used, or
    when what is left would not cover two more runs like this one.
    """
    if exhausted:
        return ("המכסה החודשית של Tavily נגמרה באמצע הריצה. חברות שלא הספיקו לקבל גודל או "
                "איש קשר יטופלו כשהמכסה תתחדש.")
    if not used or not limit:
        return ""
    left = max(limit - used, 0)
    if used >= 0.8 * limit or (run_used and left < 2 * run_used):
        return (f"נוצלו {used} מתוך {limit} חיפושים החודש ב-Tavily (נשארו {left}). "
                f"הריצה הזו השתמשה ב-{run_used}, כך שהמכסה עלולה להיגמר בקרוב.")
    return ""


def format_results(results: list[SearchResult], limit: int = 8) -> str:
    lines = []
    for i, r in enumerate(results[:limit], 1):
        lines.append(f"[{i}] {r.title}\n    {r.link}\n    {r.snippet}")
    return "\n".join(lines) or "(no results)"
