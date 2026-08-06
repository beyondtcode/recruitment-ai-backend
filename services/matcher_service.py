"""AI job-match analysis for n8n (Claude / AsyncAnthropic).

Lean analysis engine only — no Monday, DB, email, or Slack orchestration.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from core.config import settings
from schemas.matcher import (
    BatchMatchResponse,
    CandidateInfo,
    CandidateOnlyRequest,
    JobInput,
    MatchAnalysis,
    MatchRequest,
)
from services.job_scraper_service import scrape_daily_jobs

logger = logging.getLogger(__name__)

TOOL_NAME = "analyze_job_match"
MAX_TOKENS = 2048
MATCH_THRESHOLD = 60

MAX_JOBS_PER_BATCH = 15
LLM_CONCURRENCY = 5
LLM_CALL_TIMEOUT_SECONDS = 15.0
BATCH_LLM_DEADLINE_SECONDS = 40.0

# Title hits weigh more than description hits when ranking for the LLM batch.
_TITLE_SCORE_WEIGHT = 3
_DESC_SCORE_WEIGHT = 1
# Minimum soft relevance to keep a job when the candidate has extractable tech tokens.
_MIN_RELEVANCE_SCORE = 1

_WEB_PHRASES: tuple[str, ...] = (
    "full stack",
    "fullstack",
    "full-stack",
    "front end",
    "frontend",
    "front-end",
    "back end",
    "backend",
    "back-end",
    "web developer",
    "web development",
    "software engineer",
    "javascript",
    "typescript",
    "react",
    "next.js",
    "nextjs",
    "node.js",
    "nodejs",
    "nestjs",
    "angular",
    "vue.js",
    "vuejs",
    "vue",
    "django",
    "fastapi",
    "flask",
    "express",
    "spring boot",
    "spring",
    "rails",
    "ruby on rails",
    "graphql",
    "rest api",
    "microservices",
    "aws",
    "gcp",
    "azure",
    "docker",
    "kubernetes",
    "k8s",
    "postgresql",
    "postgres",
    "mongodb",
    "redis",
    "python",
    "java",
    "golang",
    ".net",
    "csharp",
    "c#",
    "php",
    "laravel",
    "html",
    "css",
    "tailwind",
    "prisma",
    "sql",
)

_EXCLUDE_PHRASES: tuple[str, ...] = (
    "c++",
    "embedded",
    "firmware",
    "kernel",
    "hardware",
    "cad",
    "ate",
    "vhdl",
    "verilog",
    "rtos",
    "fpga",
    "asic",
    "pcb",
    "soc design",
    "chip design",
    "device driver",
    "bare metal",
    "bare-metal",
    "solidworks",
    "autocad",
    "electrical engineer",
    "mechanical engineer",
    "rf engineer",
    "fpga engineer",
    "firmware engineer",
    "embedded engineer",
    "hardware engineer",
)

# Multi-word / symbol phrases first so "c++" / "next.js" match before looser tokens.
_WEB_PHRASES_SORTED = tuple(sorted(_WEB_PHRASES, key=len, reverse=True))
_EXCLUDE_PHRASES_SORTED = tuple(sorted(_EXCLUDE_PHRASES, key=len, reverse=True))

SYSTEM_PROMPT = """You are an elite Israeli tech recruiter matching candidates to job openings.

## Wide Funnel strategy
Evaluate the candidate profile against the job description generously:
- Favor transferable skills, adjacent roles, and strong overlapping experience.
- Do NOT require exact title or keyword matches to score well.
- Penalize only clear blockers (wrong seniority band, missing hard must-haves that cannot be inferred).
- Score inclusively: a plausible fit with some gaps should land in the 55–75 range; a strong fit 75–95.

## Scoring
- match_score: integer 0–100 reflecting overall fit under the Wide Funnel strategy.

## CV version selection
- selected_cv_version: pick the single best filename from available_cv_files that fits this job
  (e.g. backend vs fullstack vs Hebrew/English variants).
- If available_cv_files is empty or none is appropriate, set selected_cv_version to null.

## Reasoning & cover pitch
- reasoning: concise English explanation of the score (strengths, gaps, why Wide Funnel applies).
- cover_pitch: if match_score >= 60, write a tailored 2–3 sentence Hebrew pitch suitable for an
  email job application (professional, specific to this role/company). If match_score < 60,
  set cover_pitch to null.

## Output
Call the tool with the required fields only. Do not invent job_id, company, title, or apply_url —
those are filled by the server from the request."""

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY") or settings.anthropic_api_key
        _client = AsyncAnthropic(api_key=api_key)
    return _client


def _matcher_model() -> str:
    """Prefer the light matcher model; empty override falls back to default settings model."""
    configured = (settings.anthropic_matcher_model or "").strip()
    return configured or settings.anthropic_model


def _tool_input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "match_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall fit score 0–100 under Wide Funnel strategy.",
            },
            "selected_cv_version": {
                "type": ["string", "null"],
                "description": (
                    "Best matching filename from available_cv_files, or null if none."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": "Concise English explanation of the match score.",
            },
            "cover_pitch": {
                "type": ["string", "null"],
                "description": (
                    "2–3 sentence Hebrew email pitch when score >= 60; otherwise null."
                ),
            },
        },
        "required": ["match_score", "selected_cv_version", "reasoning", "cover_pitch"],
        "additionalProperties": False,
    }


def _parse_tool_input(response: Any) -> dict[str, Any]:
    for block in response.content:
        if block.type == "tool_use" and block.name == TOOL_NAME:
            return block.input
    raise ValueError("Claude response did not include the expected tool_use block")


def _build_user_content(request: MatchRequest) -> str:
    files = request.candidate.available_cv_files
    files_text = "\n".join(f"- {name}" for name in files) if files else "(none)"
    return (
        f"## Job\n"
        f"Title: {request.job.title}\n"
        f"Company: {request.job.company}\n"
        f"URL: {request.job.url}\n\n"
        f"### Description\n{request.job.description}\n\n"
        f"## Candidate profile\n{request.candidate.profile_text}\n\n"
        f"## Available CV files\n{files_text}\n\n"
        "Analyze the match and call the tool."
    )


def _normalize_selected_cv(
    selected: Any,
    available: list[str],
) -> str | None:
    if selected is None:
        return None
    name = str(selected).strip()
    if not name:
        return None
    if not available:
        return None
    if name in available:
        return name
    # Case-insensitive fallback
    lower_map = {f.lower(): f for f in available}
    return lower_map.get(name.lower())


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _phrase_in_text(needle: str, text: str) -> bool:
    """Match phrase with word-ish boundaries; substring for symbols (c++, .net)."""
    if not needle or not text:
        return False
    if re.search(r"[+#.]", needle) or " " in needle or "-" in needle:
        return needle in text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", text))


def _matching_phrases(text: str, phrases: tuple[str, ...]) -> set[str]:
    """Return distinct phrases from ``phrases`` found in normalized text."""
    if not text:
        return set()
    return {p for p in phrases if _phrase_in_text(p.strip().lower(), text)}


def _count_phrase_hits(text: str, phrases: tuple[str, ...]) -> int:
    return len(_matching_phrases(text, phrases))


def _candidate_is_web_leaning(profile_text: str) -> bool:
    normalized = _normalize_text(profile_text)
    web_hits = _count_phrase_hits(normalized, _WEB_PHRASES_SORTED)
    exclude_hits = _count_phrase_hits(normalized, _EXCLUDE_PHRASES_SORTED)
    return web_hits >= 2 and web_hits > exclude_hits


def _job_is_hard_domain_mismatch(job: JobInput, *, candidate_web: bool) -> bool:
    """Drop embedded/hardware/CAD roles when the candidate is clearly web/fullstack."""
    if not candidate_web:
        return False
    haystack = _normalize_text(f"{job.title} {job.description}")
    exclude_hits = _count_phrase_hits(haystack, _EXCLUDE_PHRASES_SORTED)
    web_hits = _count_phrase_hits(haystack, _WEB_PHRASES_SORTED)
    # Dominated by exclude-domain terms with little/no web overlap.
    return exclude_hits >= 2 and web_hits == 0


def _relevance_score(profile_phrases: set[str], job: JobInput) -> int:
    """Weighted overlap of candidate tech phrases vs job title/description."""
    if not profile_phrases:
        # No extractable candidate tech signal — keep all non-hard-filtered jobs equally.
        return 0
    title = _normalize_text(job.title)
    description = _normalize_text(job.description)
    title_phrases = _matching_phrases(title, _WEB_PHRASES_SORTED)
    desc_phrases = _matching_phrases(description, _WEB_PHRASES_SORTED)
    title_overlap = len(profile_phrases & title_phrases)
    desc_overlap = len(profile_phrases & desc_phrases)
    return title_overlap * _TITLE_SCORE_WEIGHT + desc_overlap * _DESC_SCORE_WEIGHT


def prefilter_jobs_for_candidate(
    candidate: CandidateInfo,
    jobs: list[JobInput],
    *,
    max_jobs: int = MAX_JOBS_PER_BATCH,
) -> list[JobInput]:
    """Hard-filter irrelevant domains, rank by keyword overlap, cap batch size."""
    if not jobs:
        return []

    candidate_web = _candidate_is_web_leaning(candidate.profile_text)
    profile_phrases = _matching_phrases(
        _normalize_text(candidate.profile_text),
        _WEB_PHRASES_SORTED,
    )

    survivors: list[tuple[int, JobInput]] = []
    hard_dropped = 0
    soft_dropped = 0

    for job in jobs:
        if _job_is_hard_domain_mismatch(job, candidate_web=candidate_web):
            hard_dropped += 1
            continue
        score = _relevance_score(profile_phrases, job)
        if profile_phrases and score < _MIN_RELEVANCE_SCORE:
            soft_dropped += 1
            continue
        survivors.append((score, job))

    survivors.sort(key=lambda pair: pair[0], reverse=True)
    selected = [job for _, job in survivors[:max_jobs]]

    logger.info(
        "matcher prefilter: scraped=%d hard_dropped=%d soft_dropped=%d "
        "after_filter=%d sent_to_llm=%d candidate_web=%s",
        len(jobs),
        hard_dropped,
        soft_dropped,
        len(survivors),
        len(selected),
        candidate_web,
    )
    return selected


async def analyze_job_match(request: MatchRequest) -> MatchAnalysis:
    """Score a candidate against a single job via Claude (Wide Funnel)."""
    model = _matcher_model()
    try:
        response = await _get_client().messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_content(request)}],
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": (
                        "Return structured job-match analysis fields for this candidate–job pair."
                    ),
                    "input_schema": _tool_input_schema(),
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
        )
        raw = _parse_tool_input(response)
    except Exception as exc:
        logger.exception("Anthropic job-match call failed: %s", exc)
        raise RuntimeError(f"Anthropic job-match analysis failed: {exc}") from exc

    try:
        match_score = int(raw.get("match_score", 0))
    except (TypeError, ValueError) as exc:
        logger.error("Invalid match_score from Claude: %r", raw.get("match_score"))
        raise ValueError(f"Invalid match_score from Claude: {raw.get('match_score')!r}") from exc

    match_score = max(0, min(100, match_score))
    selected = _normalize_selected_cv(
        raw.get("selected_cv_version"),
        request.candidate.available_cv_files,
    )
    reasoning = str(raw.get("reasoning") or "").strip()
    if not reasoning:
        raise ValueError("Claude returned empty reasoning")

    cover_raw = raw.get("cover_pitch")
    cover_pitch: str | None
    if cover_raw is None:
        cover_pitch = None
    else:
        cover_pitch = str(cover_raw).strip() or None

    if match_score < MATCH_THRESHOLD:
        cover_pitch = None

    try:
        return MatchAnalysis(
            job_id=request.job.id,
            company=request.job.company,
            title=request.job.title,
            match_score=match_score,
            passed_threshold=match_score >= MATCH_THRESHOLD,
            selected_cv_version=selected,
            reasoning=reasoning,
            cover_pitch=cover_pitch,
            apply_url=request.job.url,
        )
    except ValidationError as exc:
        logger.error("MatchAnalysis validation failed: %s", exc)
        raise ValueError(f"Failed to build MatchAnalysis: {exc}") from exc


async def get_daily_jobs() -> list[JobInput]:
    """Return newly scraped Israeli tech jobs for matching (30-day dedup)."""
    try:
        result = await scrape_daily_jobs()
        logger.info(
            "get_daily_jobs: %d total, %d new after dedup",
            result.total_found,
            result.new_jobs_count,
        )
        return result.jobs
    except Exception:
        logger.exception("get_daily_jobs: scrape failed")
        return []


async def _analyze_one_with_limits(
    candidate: CandidateInfo,
    job: JobInput,
    semaphore: asyncio.Semaphore,
) -> MatchAnalysis | None:
    """Run a single match under concurrency + per-call timeout; soft-fail on errors."""
    async with semaphore:
        try:
            return await asyncio.wait_for(
                analyze_job_match(MatchRequest(candidate=candidate, job=job)),
                timeout=LLM_CALL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Skipping job %s (%s @ %s): LLM call timed out after %.0fs",
                job.id,
                job.title,
                job.company,
                LLM_CALL_TIMEOUT_SECONDS,
            )
            return None
        except Exception:
            logger.exception(
                "Skipping job %s (%s @ %s) due to match analysis failure",
                job.id,
                job.title,
                job.company,
            )
            return None


async def _run_batch_llm(
    candidate: CandidateInfo,
    jobs: list[JobInput],
) -> list[MatchAnalysis]:
    """Score jobs in parallel (Semaphore) with a hard batch deadline."""
    if not jobs:
        return []

    semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
    tasks = [
        asyncio.create_task(_analyze_one_with_limits(candidate, job, semaphore))
        for job in jobs
    ]

    done: set[asyncio.Task[MatchAnalysis | None]]
    pending: set[asyncio.Task[MatchAnalysis | None]]
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=BATCH_LLM_DEADLINE_SECONDS,
            return_when=asyncio.ALL_COMPLETED,
        )
    except Exception:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if pending:
        logger.warning(
            "Batch LLM deadline (%.0fs) reached: completed=%d pending=%d; cancelling rest",
            BATCH_LLM_DEADLINE_SECONDS,
            len(done),
            len(pending),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    matches: list[MatchAnalysis] = []
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            logger.error("Batch match task failed: %s", exc, exc_info=exc)
            continue
        analysis = task.result()
        if analysis is not None and analysis.passed_threshold:
            matches.append(analysis)

    matches.sort(key=lambda m: m.match_score, reverse=True)
    return matches


async def analyze_candidate_against_all_jobs(
    request: CandidateOnlyRequest,
) -> BatchMatchResponse:
    """Match one candidate against daily jobs; prefilter then parallel LLM scoring."""
    jobs = await get_daily_jobs()
    if not jobs:
        return BatchMatchResponse(
            candidate_email=request.candidate_email,
            total_scanned=0,
            matches_found=0,
            matches=[],
        )

    selected = prefilter_jobs_for_candidate(request.candidate, jobs)
    started = time.perf_counter()
    matches = await _run_batch_llm(request.candidate, selected)
    elapsed = time.perf_counter() - started

    logger.info(
        "Batch match complete: scanned=%d llm_jobs=%d matches=%d llm_elapsed=%.1fs model=%s",
        len(jobs),
        len(selected),
        len(matches),
        elapsed,
        _matcher_model(),
    )

    return BatchMatchResponse(
        candidate_email=request.candidate_email,
        total_scanned=len(jobs),
        matches_found=len(matches),
        matches=matches,
    )
