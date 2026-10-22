"""LLM enrichment for Lead Scraping Agent (category + summary).

Env vars:
  ANTHROPIC_API_KEY / ANTHROPIC_MODEL — preferred provider (via core.config)
  OPENAI_API_KEY / OPENAI_MODEL — fallback when Anthropic key is absent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Literal

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from core.config import settings
from services.lead_filters import normalize_category, outreach_phrasing
from services.scraper import (
    _clean_job_link,
    _normalize_job_title,
)

logger = logging.getLogger(__name__)

Provider = Literal["anthropic", "openai"]

MAX_TOKENS = 512
CONCURRENCY_LIMIT = 5
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You classify Israeli job postings for a recruitment and outsourcing agency
(Beyond Code) that places software developers, QA/automation testers, Salesforce /
monday.com implementers and AI Builders.

Given a company name, job title and an optional listing snippet, return ONLY a JSON object:
- "category": exactly one of the values below, or null if the role fits none of them.
    "Backend"        — backend / server-side developer (Python, Java, Node, .NET, Go developer)
    "Frontend"       — frontend / UI developer (React, Angular, Vue)
    "Full Stack"     — full stack developer
    "QA"             — manual QA, QA engineer, software tester (בודק/ת תוכנה)
    "פיתוח אוטומציה" — test automation developer/engineer, QA automation, SDET
    "יישום Salesforce" — Salesforce developer / admin / consultant / implementer
    "יישום monday.com" — monday.com implementation / expert / consultant / admin
    "AI Builder"     — titled AI Builder
    "AI Workflow Builder" — builds AI workflows (n8n/Make/Zapier + LLMs)
    "AI Automation Specialist" — AI automation specialist / engineer
    "AI Implementation Specialist" — AI implementation specialist / engineer
    "AI Agent Developer" — AI agent developer/engineer, agentic AI developer
  Generic "Software Engineer/Developer" with no clear side → "Full Stack" if both sides are
  mentioned, otherwise "Backend".
  Return null for: DevOps/SRE, data scientist, data engineer, ML research, product manager,
  cyber/security, IT/support, hardware/embedded/firmware/chip, mobile-only roles that are not
  web development, managers without hands-on development, and any non-tech role
  (e.g. fashion "product developer", sales, marketing).
- "job_summary": one short sentence describing the role/requirements from the snippet
  (or the title alone if the snippet is empty). Do not invent details.

Do not include markdown or extra keys."""

USER_PROMPT_TEMPLATE = (
    "Company: {company_name}\n"
    "Job title: {job_title}\n"
    "Listing snippet:\n{snippet}\n\n"
    "Respond with JSON only."
)


_anthropic_client: AsyncAnthropic | None = None
_openai_client: AsyncOpenAI | None = None


def _get_anthropic_client() -> AsyncAnthropic:
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
        _anthropic_client = AsyncAnthropic(api_key=api_key)
    return _anthropic_client


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


def _resolve_provider() -> Provider | None:
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or getattr(settings, "anthropic_api_key", "") or "").strip()
    if anthropic_key:
        return "anthropic"
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if openai_key:
        return "openai"
    return None


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM response JSON is not an object")
    return data


def _normalize_job_summary(raw: Any, job_title: str) -> str:
    if raw is None:
        text = ""
    else:
        text = " ".join(str(raw).split()).strip()
    if text:
        return text
    title = (job_title or "").strip()
    if title:
        return f"Opening for {title}."
    return "Software / tech role opening."


def _normalize_classification(
    payload: dict[str, Any],
    *,
    job_title: str = "",
    snippet: str = "",
) -> dict[str, Any]:
    if "category" not in payload:
        raise ValueError(f"Invalid classification payload: {payload!r}")
    return {
        "category": normalize_category(payload.get("category")),
        "job_summary": _normalize_job_summary(payload.get("job_summary"), job_title),
    }


def _user_prompt(company_name: str, job_title: str, snippet: str) -> str:
    snippet_text = (snippet or "").strip() or "(none)"
    return USER_PROMPT_TEMPLATE.format(
        company_name=company_name,
        job_title=job_title,
        snippet=snippet_text,
    )


async def complete_json(system: str, user: str, *, max_tokens: int = MAX_TOKENS) -> dict[str, Any]:
    """Run one LLM call with the configured provider and parse a JSON object reply."""
    provider = _resolve_provider()
    if provider is None:
        raise RuntimeError("No LLM API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    if provider == "anthropic":
        response = await _get_anthropic_client().messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "\n".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text" and getattr(block, "text", None)
        )
    else:
        model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        response = await _get_openai_client().chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        text = response.choices[0].message.content if response.choices else ""
    if not text:
        raise ValueError("LLM response had no text content")
    return _extract_json_object(text)


async def _classify_with_anthropic(
    company_name: str,
    job_title: str,
    snippet: str,
) -> dict[str, Any]:
    response = await _get_anthropic_client().messages.create(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _user_prompt(company_name, job_title, snippet),
            }
        ],
    )
    text_parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    if not text_parts:
        raise ValueError("Anthropic response had no text content")
    return _normalize_classification(
        _extract_json_object("\n".join(text_parts)),
        job_title=job_title,
        snippet=snippet,
    )


async def _classify_with_openai(
    company_name: str,
    job_title: str,
    snippet: str,
) -> dict[str, Any]:
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    response = await _get_openai_client().chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _user_prompt(company_name, job_title, snippet),
            },
        ],
    )
    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise ValueError("OpenAI response had no content")
    return _normalize_classification(
        _extract_json_object(content),
        job_title=job_title,
        snippet=snippet,
    )


async def _classify_job(
    provider: Provider,
    company_name: str,
    job_title: str,
    snippet: str,
) -> dict[str, Any]:
    if provider == "anthropic":
        return await _classify_with_anthropic(company_name, job_title, snippet)
    return await _classify_with_openai(company_name, job_title, snippet)


async def _enrich_one(
    job: dict[str, Any],
    provider: Provider,
    semaphore: asyncio.Semaphore,
) -> tuple[str, dict[str, Any]]:
    """Return (status, job) where status is "kept", "rejected" or "error"."""
    job_link = _clean_job_link(job.get("job_link") or "")
    company_name = " ".join((job.get("company_name") or "").split()).strip() or "Unknown"
    job_title = _normalize_job_title(job.get("job_title") or "")
    snippet = (job.get("snippet") or "").strip()
    if not job_link or not job_title:
        logger.warning("enrich_jobs: skipping job without link/title: %r", job)
        return "rejected", job

    async with semaphore:
        try:
            classification = await _classify_job(provider, company_name, job_title, snippet)
        except Exception:
            logger.exception(
                "enrich_jobs: LLM failed for company=%r title=%r", company_name, job_title
            )
            return "error", job

    category = classification["category"]
    if category is None:
        logger.info(
            "enrich_jobs: no matching category company=%r title=%r", company_name, job_title
        )
        return "rejected", job

    enriched = dict(job)
    enriched["company_name"] = company_name
    enriched["job_title"] = job_title
    enriched["job_link"] = job_link
    enriched["category"] = category
    enriched["outreach_phrasing"] = outreach_phrasing(category)
    enriched["job_summary"] = classification["job_summary"]
    return "kept", enriched


async def classify_lead_jobs(jobs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Classify jobs into the 12 lead categories.

    Returns ``(kept, settled)``: ``kept`` are jobs with a category; ``settled``
    are all jobs with a definitive answer (kept + rejected), safe to mark as seen.
    Jobs whose LLM call failed are in neither list so the next run retries them.
    """
    if not jobs:
        return [], []

    provider = _resolve_provider()
    if provider is None:
        logger.error(
            "enrich_jobs: no LLM API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY); returning []"
        )
        return [], []

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    results = await asyncio.gather(*(_enrich_one(job, provider, semaphore) for job in jobs))
    kept = [j for status, j in results if status == "kept"]
    settled = [j for status, j in results if status in ("kept", "rejected")]
    logger.info(
        "enrich_jobs: %d in → %d kept, %d rejected, %d errors (provider=%s)",
        len(jobs),
        len(kept),
        len(settled) - len(kept),
        len(jobs) - len(settled),
        provider,
    )
    return kept, settled


async def enrich_jobs(jobs: list[dict]) -> list[dict]:
    """Backward-compatible wrapper: classified jobs only."""
    kept, _ = await classify_lead_jobs(jobs)
    return kept
