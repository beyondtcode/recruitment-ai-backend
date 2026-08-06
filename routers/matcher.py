"""CV Tailoring & Job Matcher routes for n8n."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from schemas.matcher import (
    BatchMatchResponse,
    CandidateOnlyRequest,
    MatchAnalysis,
    MatchRequest,
)
from services.matcher_service import (
    analyze_candidate_against_all_jobs,
    analyze_job_match,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["matcher"])


@router.post("/analyze-match", response_model=MatchAnalysis)
async def analyze_match(body: MatchRequest) -> MatchAnalysis:
    """Score a candidate against a job; returns MatchAnalysis for n8n."""
    try:
        return await analyze_job_match(body)
    except ValidationError as exc:
        logger.warning("Match analysis validation error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="AI returned an invalid match analysis payload",
        ) from exc
    except ValueError as exc:
        logger.warning("Match analysis parse error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to parse AI match analysis: {exc}",
        ) from exc
    except RuntimeError as exc:
        logger.error("Match analysis upstream failure: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected match analysis failure: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during job match analysis",
        ) from exc


@router.post("/analyze-candidate-jobs", response_model=BatchMatchResponse)
async def analyze_candidate_jobs(body: CandidateOnlyRequest) -> BatchMatchResponse:
    """Match a candidate against all daily jobs; return threshold-passing matches."""
    try:
        return await analyze_candidate_against_all_jobs(body)
    except ValidationError as exc:
        logger.warning("Batch match validation error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="AI returned an invalid batch match analysis payload",
        ) from exc
    except ValueError as exc:
        logger.warning("Batch match parse error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to parse AI batch match analysis: {exc}",
        ) from exc
    except RuntimeError as exc:
        logger.error("Batch match upstream failure: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception("Unexpected batch match failure: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="Unexpected error during candidate job matching",
        ) from exc
