"""AI job-match analysis for n8n (Claude / AsyncAnthropic).

Lean analysis engine only — no Monday, DB, email, or Slack orchestration.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from core.config import settings
from schemas.matcher import (
    BatchMatchResponse,
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


async def analyze_job_match(request: MatchRequest) -> MatchAnalysis:
    """Score a candidate against a single job via Claude (Wide Funnel)."""
    try:
        response = await _get_client().messages.create(
            model=settings.anthropic_model,
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


async def analyze_candidate_against_all_jobs(
    request: CandidateOnlyRequest,
) -> BatchMatchResponse:
    """Match one candidate against all daily jobs; return threshold-passing hits."""
    jobs = await get_daily_jobs()
    if not jobs:
        return BatchMatchResponse(
            candidate_email=request.candidate_email,
            total_scanned=0,
            matches_found=0,
            matches=[],
        )

    matches: list[MatchAnalysis] = []
    for job in jobs:
        try:
            analysis = await analyze_job_match(
                MatchRequest(candidate=request.candidate, job=job)
            )
        except Exception:
            logger.exception(
                "Skipping job %s (%s @ %s) due to match analysis failure",
                job.id,
                job.title,
                job.company,
            )
            continue

        if analysis.passed_threshold:
            matches.append(analysis)

    matches.sort(key=lambda m: m.match_score, reverse=True)
    return BatchMatchResponse(
        candidate_email=request.candidate_email,
        total_scanned=len(jobs),
        matches_found=len(matches),
        matches=matches,
    )
