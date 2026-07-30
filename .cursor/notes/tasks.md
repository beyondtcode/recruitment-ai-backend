# Recruitment AI Backend — Task Tracker

> Last updated: 2026-06-30  
> Legend: ✅ Done · 🔄 In progress / recently touched · ⬜ Remaining

---

## ✅ Completed — Core Platform

- [x] FastAPI application with lifespan-managed APScheduler (`app.py`)
- [x] Health check endpoint (`GET /health`)
- [x] Render.com deployment config (`render.yaml`)
- [x] Pydantic Settings for Anthropic + Main Hub board (`core/config.py`)
- [x] Shared Monday GraphQL client (`services/monday_service.py` → `post_graphql`)
- [x] Comprehensive README with architecture diagrams and env var tables

---

## ✅ Completed — CV Pipeline

- [x] PDF/DOCX text extraction with hyperlink preservation (`utils/file_parser.py`)
- [x] Monday signed-URL CV download (`download_cv_from_url`)
- [x] `CandidateSchema` with Monday column ID mapping (`models/candidate.py`)
- [x] Claude structured extraction via tool calling (`services/ai_service.py`)
- [x] Validation retry on Pydantic errors (single retry)
- [x] Post-extraction sanitizers (test_score, interview_summaries, city notes, programming language cap, etc.)
- [x] Shared CV orchestrator (`services/cv_pipeline.py`) — used by email, webhook, tests
- [x] Monday candidate upsert by email/phone (`upsert_candidate_item`)
- [x] Main Hub sync when webhook originates from a job board
- [x] Monday CV webhook handler (`POST /monday-webhook`) with challenge echo, custom app payload support, background processing
- [x] IMAP email ingestion (`services/email_service.py`)
- [x] Daily email CV batch with attachment validation, CV heuristics, Message-ID + hash dedup (`services/email_batch.py`)
- [x] CLI entry for same-day email processing (`main.py`)
- [x] Scheduled email batch at 08:00 Asia/Jerusalem
- [x] Extraction confidence + reasoning (debug-only, not written to Monday)
- [x] Low-confidence skip when no identity fields present

---

## ✅ Completed — Job-Fit Scoring (Job Boards)

- [x] Fetch דרישות משרה from job board static info item (`fetch_board_job_requirements`)
- [x] `job_fit_score` (1–10) and `job_fit_reasoning` fields on `CandidateSchema`
- [x] AI prompt rules: hard-requirement gate (max 3/10 if any חובה fails)
- [x] Sanitizer clears job-fit fields when no requirements context
- [x] `apply_job_fit_columns()` writes score/reasoning to job-template columns only (not Main Hub)
- [x] Tests for job-fit fetch, format, and column application (`test_monday_upsert.py`, `test_ai_sanitize.py`)

---

## ✅ Completed — CRM Meeting Pipeline

- [x] CRM-specific settings (`crm_integration/config.py` — `CrmSettings`)
- [x] NodeTaker webhook payload schemas (`crm_integration/schemas.py`)
- [x] FastAPI router for CRM routes (`crm_integration/routes.py`)
- [x] Contact lookup by participant email — clients first, then leads (`lookup.py`)
- [x] Meeting item creation with type classification (`meeting.py`)
- [x] Workdoc creation with markdown block parsing (`workdoc.py`)
- [x] Client/lead AI profile update from meeting summary (`contact_profile.py`)
- [x] End-to-end CRM orchestrator (`pipeline.py`)
- [x] Monday Notetaker API fetcher (`monday_fetcher.py`)
- [x] Nightly Notetaker batch with wake webhook (`batch.py`, `POST /run-notetaker-batch`)
- [x] `X-Batch-Secret` auth on batch wake endpoint
- [x] Morning briefing batch at 07:00 (`process_morning_briefs`)
- [x] Mirly reminders integration (`crm_integration/reminders.py`)
- [x] Past meeting context gathering for profile updates (`gather_past_meeting_context`)
- [x] Dev test endpoint `GET /test-fetch-sarah`
- [x] Extensive CRM test suite (`test_crm_nodetaker_webhook.py`)

---

## ✅ Completed — Testing & Quality

- [x] CV pipeline integration tests (`test_pipeline.py`)
- [x] Webhook parsing tests (`test_webhook.py`)
- [x] File parser edge cases — tables, headers, hyperlinks (`test_file_parser.py`)
- [x] Email batch gates and dedup tests (`test_email_batch.py`)
- [x] Monday connectivity smoke test (`test_connection.py`)

---

## 🔄 In Progress / Recently Modified (uncommitted work)

Based on current working tree (2026-06-30):

- [ ] 🔄 `crm_integration/config.py` — CRM board/column ID updates
- [ ] 🔄 `crm_integration/meeting.py` — meeting creation / classification tweaks
- [ ] 🔄 `crm_integration/monday_client.py` — GraphQL query changes
- [ ] 🔄 `services/ai_service.py` — prompt / sanitizer refinements
- [ ] 🔄 `services/cv_pipeline.py` — pipeline behavior adjustments
- [ ] 🔄 `services/monday_service.py` — upsert / column mapping changes
- [ ] 🔄 `test_crm_nodetaker_webhook.py` — test updates for CRM changes
- [ ] 🔄 `test_monday_upsert.py` — upsert / job-fit test updates
- [ ] 🔄 `sync_to_ofer_board.py` — ad-hoc board sync script (not integrated into main app)
- [ ] 🔄 `crm_integration/brief_manager.py` — referenced in pycache but source file missing from tree (verify / restore)

---

## ⬜ Remaining — Reliability & Operations

- [ ] Structured logging (JSON) for production log aggregation
- [ ] Webhook signature verification for `/nodetaker-webhook` (shared secret or HMAC)
- [ ] Rate limiting / idempotency keys for webhook endpoints
- [ ] Dead-letter or retry queue for failed CV/CRM processing (currently fire-and-forget background tasks)
- [ ] Health check that verifies Monday + Anthropic connectivity (not just process liveness)
- [ ] Remove or protect `GET /test-fetch-sarah` in production
- [ ] CI pipeline (GitHub Actions) running `pytest` on push
- [ ] Pin dependency versions in `requirements.txt` (currently minimum versions only)
- [ ] Migrate email dedup state off local filesystem (ephemeral Render disk resets on deploy)

---

## ⬜ Remaining — CV Pipeline Enhancements

- [ ] Support additional file types (`.doc`, images with OCR) if business requires
- [ ] Configurable low-confidence policy (warn vs. skip vs. create draft item)
- [ ] Metrics dashboard: attachments processed, created/updated/skipped/error counts over time
- [ ] Batch re-processing tool for historical CVs on a board
- [ ] Admin endpoint to trigger email batch on demand (with auth)

---

## ⬜ Remaining — CRM Enhancements

- [ ] Create meeting items even when no client/lead match (currently skips on no match in some paths — confirm desired behavior)
- [ ] Bi-directional sync: Monday status changes → external notifications
- [ ] Reminder deduplication hardening across timezone edge cases
- [ ] Unified batch status reporting endpoint (last run summary persisted)

---

## ⬜ Remaining — RAG / Search Layer (not started)

The codebase today does **not** implement RAG. If the broader project goal includes recruiter-facing Q&A or semantic candidate search, these tasks are outstanding:

- [ ] Choose vector store and embedding model (see `decisions.md`)
- [ ] Chunking strategy for CVs (by section: experience, skills, education)
- [ ] Chunking strategy for meeting transcripts / Workdocs
- [ ] Embedding pipeline triggered on CV upsert and meeting creation
- [ ] Metadata filters (board_id, job_category, city, years_of_experience, date range)
- [ ] Retrieval API: `POST /search/candidates`, `POST /ask` with cited sources
- [ ] Hybrid search: structured Monday filters + semantic similarity
- [ ] Evaluation set: labeled queries with expected candidate IDs (recall@k)
- [ ] PII/redaction policy for chunks stored outside Monday

---

## ⬜ Remaining — Auth & Multi-Tenancy (if productized)

- [ ] API authentication for any new user-facing endpoints
- [ ] Per-agency Monday workspace isolation
- [ ] Role-based access (recruiter vs. admin)

---

## Suggested Priority Order

1. **Stabilize in-flight CRM/CV changes** — commit and test modified modules
2. **Operational hardening** — webhook auth, persistent dedup, CI
3. **RAG foundation** — embedding pipeline on CV upsert events (highest product value for “search” use cases)
4. **Recruiter Q&A API** — retrieval + Claude generation with citations
5. **Productization** — auth, metrics, admin tools
