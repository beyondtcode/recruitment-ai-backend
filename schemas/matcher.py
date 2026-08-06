"""Pydantic schemas for the CV Tailoring & Job Matcher endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateInfo(BaseModel):
    profile_text: str
    available_cv_files: list[str] = Field(default_factory=list)


class JobInput(BaseModel):
    id: str
    title: str
    company: str
    description: str
    url: str


class MatchRequest(BaseModel):
    candidate: CandidateInfo
    job: JobInput


class MatchAnalysis(BaseModel):
    job_id: str
    company: str
    title: str
    match_score: int = Field(ge=0, le=100)
    passed_threshold: bool
    selected_cv_version: str | None = None
    reasoning: str
    cover_pitch: str | None = None
    apply_url: str


class CandidateOnlyRequest(BaseModel):
    candidate: CandidateInfo
    candidate_email: str | None = None


class BatchMatchResponse(BaseModel):
    candidate_email: str | None = None
    total_scanned: int
    matches_found: int
    matches: list[MatchAnalysis] = Field(default_factory=list)
