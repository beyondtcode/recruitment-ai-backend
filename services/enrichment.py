"""LLM enrichment for Lead Scraping Agent (company size + track).

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

logger = logging.getLogger(__name__)

Provider = Literal["anthropic", "openai"]

MAX_TOKENS = 256
CONCURRENCY_LIMIT = 5
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

SYSTEM_PROMPT = """You classify job postings for a recruitment agency that ONLY places
Software Development and AI/ML engineers (not fashion, retail, textile, or industrial
"product developer" roles).

Given a company name and job title, return ONLY a JSON object with:
- "company_size": exactly "100+" if the company likely has 100 or more employees, otherwise exactly "<100"
- "track": "B" if the role is primarily AI / ML / LLM / Data Science; otherwise "A" (Software Development / Tech)
- "is_tech_role": true only if this is a real software engineering, software development, or AI/ML/data role.
  Set false for non-tech product roles (e.g. fashion/textile/apparel product developer at brands like
  Golf, ADIKA, Castro), industrial/physical product design, merchandising, or similar.

Use your knowledge of well-known companies when possible. If unsure about size, prefer "<100".
If unsure whether the role is tech, prefer is_tech_role=false for ambiguous "product developer" titles
without clear software/engineering signals.
Do not include markdown or extra keys."""

USER_PROMPT_TEMPLATE = (
    'Company: {company_name}\nJob title: {job_title}\n\nRespond with JSON only.'
)

_SIZE_PLUS_RE = re.compile(r"100\s*\+|>=?\s*100|100\s*or\s*more", re.IGNORECASE)
_SIZE_UNDER_RE = re.compile(r"<\s*100|under\s*100|less\s*than\s*100", re.IGNORECASE)

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


def _normalize_company_size(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text in ("100+", "<100"):
        return text
    if _SIZE_PLUS_RE.search(text):
        return "100+"
    if _SIZE_UNDER_RE.search(text):
        return "<100"
    # Numeric ranges like "51-200" / "201-500"
    nums = [int(n) for n in re.findall(r"\d+", text)]
    if nums:
        return "100+" if max(nums) >= 100 else "<100"
    return None


def _normalize_track(raw: Any) -> Literal["A", "B"] | None:
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if text in ("A", "B"):
        return text  # type: ignore[return-value]
    if any(token in text for token in ("AI", "ML", "LLM", "DATA SCIENCE", "DATA SCIENTIST")):
        return "B"
    if text:
        return "A"
    return None


def _normalize_is_tech_role(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return True  # backward-compatible if model omits the field
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return True


def _normalize_classification(payload: dict[str, Any]) -> dict[str, Any]:
    company_size = _normalize_company_size(payload.get("company_size"))
    track = _normalize_track(payload.get("track"))
    if company_size is None or track is None:
        raise ValueError(f"Invalid classification payload: {payload!r}")
    return {
        "company_size": company_size,
        "track": track,
        "is_tech_role": _normalize_is_tech_role(payload.get("is_tech_role")),
    }


async def _classify_with_anthropic(company_name: str, job_title: str) -> dict[str, Any]:
    response = await _get_anthropic_client().messages.create(
        model=settings.anthropic_model,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    company_name=company_name,
                    job_title=job_title,
                ),
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
    return _normalize_classification(_extract_json_object("\n".join(text_parts)))


async def _classify_with_openai(company_name: str, job_title: str) -> dict[str, Any]:
    model = os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    response = await _get_openai_client().chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_PROMPT_TEMPLATE.format(
                    company_name=company_name,
                    job_title=job_title,
                ),
            },
        ],
    )
    content = response.choices[0].message.content if response.choices else None
    if not content:
        raise ValueError("OpenAI response had no content")
    return _normalize_classification(_extract_json_object(content))


async def _classify_job(
    provider: Provider,
    company_name: str,
    job_title: str,
) -> dict[str, Any]:
    if provider == "anthropic":
        return await _classify_with_anthropic(company_name, job_title)
    return await _classify_with_openai(company_name, job_title)


async def _enrich_one(
    job: dict[str, Any],
    provider: Provider,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any] | None:
    company_name = (job.get("company_name") or "").strip() or "Unknown"
    job_title = (job.get("job_title") or "").strip()
    if not job_title:
        logger.warning("enrich_jobs: skipping job with empty title: %r", job)
        return None

    async with semaphore:
        try:
            classification = await _classify_job(provider, company_name, job_title)
        except Exception:
            logger.exception(
                "enrich_jobs: LLM failed for company=%r title=%r",
                company_name,
                job_title,
            )
            return None

    if not classification.get("is_tech_role", True):
        logger.info(
            "enrich_jobs: rejected non-tech role company=%r title=%r",
            company_name,
            job_title,
        )
        return None

    if classification["company_size"] != "100+":
        logger.debug(
            "enrich_jobs: filtered <100 company=%r title=%r",
            company_name,
            job_title,
        )
        return None

    enriched = dict(job)
    enriched["company_name"] = company_name
    enriched["job_title"] = job_title
    enriched["company_size"] = classification["company_size"]
    enriched["track"] = classification["track"]
    enriched.setdefault("publish_date", job.get("publish_date") or "")
    return enriched


async def enrich_jobs(jobs: list[dict]) -> list[dict]:
    """Classify scraped jobs with an LLM and keep only companies estimated at 100+.

    Returns dicts with original scrape fields plus ``company_size`` and ``track``.
    Soft-fails per job so one LLM error does not abort the batch.
    """
    if not jobs:
        return []

    provider = _resolve_provider()
    if provider is None:
        logger.error(
            "enrich_jobs: no LLM API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY); returning []"
        )
        return []

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    results = await asyncio.gather(
        *(_enrich_one(job, provider, semaphore) for job in jobs),
    )
    enriched = [row for row in results if row is not None]
    logger.info(
        "enrich_jobs: %d in → %d kept (provider=%s)",
        len(jobs),
        len(enriched),
        provider,
    )
    return enriched
