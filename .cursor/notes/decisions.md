# Technical Decisions & Recommendations

> Last updated: 2026-06-30  
> Context: Recruitment AI backend for a Monday.com–centric agency workflow, with a stated RAG direction.

This document records **recommended decisions**, trade-offs, and rationale. Items marked **(current)** describe what the codebase does today.

---

## 1. System of Record: Monday.com as CRM **(current)**

**Decision:** Keep Monday.com as the authoritative store for candidates, jobs, clients, leads, and meetings.

**Why:**
- Recruiters already work in Monday; duplicating state in Postgres adds sync complexity.
- The existing `CandidateSchema` → `monday_id` mapping is a clean contract.
- GraphQL upsert patterns are already battle-tested in `monday_service.py`.

**Trade-off:** Querying across boards at scale is awkward; complex analytics and full-text search are poor fits for Monday alone.

**Recommendation:** Monday remains source of truth for **structured** fields. Add a **search index** (vector DB) as a derived, eventually-consistent read model — not a second CRM.

---

## 2. AI Pattern: Structured Extraction vs. RAG **(current: extraction)**

**Decision (current):** Use Claude with a strict tool schema (`extract_candidate_fields`) and Pydantic validation — not retrieval-augmented generation.

**Why it works here:**
- CV → fields is a **mapping** problem with a fixed schema, not an open-ended Q&A problem.
- Validation + sanitizers give deterministic guardrails (e.g. never populate `interview_summaries` from CVs).
- Job-fit scoring injects requirements **directly into the prompt** when available — no retrieval needed for that path.

**When to add RAG:**
| Use case | Extraction (current) | RAG (recommended add-on) |
|----------|---------------------|--------------------------|
| Parse CV into Monday columns | ✅ | ❌ overkill |
| Score CV against job requirements on same board | ✅ (requirements fetched from board) | Optional |
| “Find candidates like X” across thousands of CVs | ❌ | ✅ |
| “What did we discuss with Acme about backend hiring?” | ❌ | ✅ |
| Recruiter chat over corpus | ❌ | ✅ |

**Recommendation:** Keep extraction pipeline as-is. Layer RAG as a **separate read path** that indexes Monday item snapshots + raw CV text + meeting summaries.

---

## 3. Vector Database Choice

**Recommendation:** **PostgreSQL + pgvector** (or **Supabase**) for this project size and team.

| Option | Pros | Cons | Fit |
|--------|------|------|-----|
| **pgvector (Postgres)** | One DB for metadata + vectors; SQL filters; mature ops; easy Render/Supabase hosting | Requires embedding pipeline + migrations | ⭐ **Best default** |
| **Qdrant** | Purpose-built ANN, good hybrid search, self-host or cloud | Another service to operate | Good if search becomes core product |
| **Pinecone** | Fully managed, fast to prototype | Cost at scale; vendor lock-in | Good for fast MVP, revisit later |
| **Chroma (embedded)** | Zero infra locally | Not ideal for Render ephemeral disks / multi-instance | Dev only |

**Why pgvector over a dedicated vector DB now:**
- You already have **no database** — adding Postgres gives you dedup state, job run logs, embedding versioning, and vectors in one place.
- Metadata filters (board_id, job_category, city, is_haredi, years_of_experience) map naturally to SQL `WHERE` + vector `<=>`.
- Render and Supabase both offer managed Postgres with pgvector.

**Schema sketch:**
```sql
documents (id, source_type, monday_item_id, board_id, raw_text, created_at)
chunks (id, document_id, chunk_index, text, embedding vector(1536), metadata jsonb)
```

---

## 4. Embedding Model

**Recommendation:** **OpenAI `text-embedding-3-small`** or **Voyage `voyage-3-lite`** for cost/quality balance; evaluate **Cohere embed-multilingual-v3.0** if Hebrew-heavy retrieval quality is weak.

**Why not Claude embeddings:** Anthropic does not offer a dedicated embedding API; keep Claude for generation/extraction, separate provider for embeddings.

**Practical note:** CVs are bilingual (Hebrew/English). Prefer a multilingual embedding model and test recall on Hebrew job titles and city names.

---

## 5. Chunking Strategy for RAG

**Recommendation:**

| Document type | Strategy |
|---------------|----------|
| CV (PDF/DOCX) | Section-aware chunks: header/contact (1 chunk), each job entry (1 chunk), skills block (1 chunk), education (1 chunk). Max ~512 tokens with 50-token overlap. |
| Meeting summary | Chunk by topic paragraph; attach metadata: `client_id`, `meeting_date`, `participant_emails` |
| Job requirements | Single chunk per job board info item; refresh on board update webhook |

**Store alongside each chunk:**
- `monday_item_id`, `board_id`, `document_type`, `candidate_email`, `extraction_confidence`
- For meetings: `contact_match_type` (lead)

**Recommendation:** Do **not** chunk the structured JSON output alone — index **raw CV text** plus optional `ai_summary` as a separate high-signal chunk.

---

## 6. Retrieval Pattern

**Recommendation:** **Hybrid search** — structured pre-filter + vector similarity.

```
User query
  → embed query
  → SQL filter (board, min years, city, job_category)   # Monday-derived metadata
  → vector top-k (e.g. k=20)
  → optional rerank (Cohere rerank or cross-encoder)
  → Claude generates answer with cited chunk IDs
```

**Why hybrid:** Recruiters often search with constraints (“5+ years Python, Jerusalem, Haredi sector”). Pure vector search misses hard filters; pure SQL misses semantic similarity (“React hooks expert” vs. literal keyword).

---

## 7. Auth System

**Current:** No auth on most endpoints; `X-Batch-Secret` only on `/run-notetaker-batch`.

**Recommendation (phased):**

| Phase | Approach |
|-------|----------|
| **Now** | Add shared-secret or HMAC verification on **all** webhooks (`/monday-webhook`, `/nodetaker-webhook`) |
| **RAG API** | API keys per integration (Monday custom app, internal dashboard) stored in Postgres |
| **Future UI** | OAuth2 via Auth0 / Clerk if a recruiter-facing app is built |

**Do not** build custom username/password auth unless required — outsource identity.

**Why:** Webhooks are already public URLs; unsigned webhooks are a spoofing risk.

---

## 8. Background Jobs & Scheduling

**Current:** APScheduler inside the FastAPI process; external cron wakes server for Notetaker batch.

**Recommendation (short term):** Keep APScheduler for 07:00/08:00 jobs **if** Render instance stays awake 24/7.

**Recommendation (medium term):** Move heavy/async work to a **task queue** (Celery + Redis, or ARQ) because:
- BackgroundTasks die on process restart
- No retry/backoff for failed Claude or Monday calls
- RAG embedding jobs will be long-running

**Render-specific:** Free/starter tiers sleep — the `/run-notetaker-batch` wake pattern exists for this. Document that in-process cron is unreliable on sleeping instances unless always-on plan.

---

## 9. Deduplication & Idempotency Storage

**Current:** Local JSON file (`temp_received_cvs/.processed_attachments.json`) for email dedup.

**Recommendation:** Move to **Postgres** (same DB as vectors) or **Redis** with TTL.

| Store | Use |
|-------|-----|
| Postgres | Durable dedup keys, batch run audit log, embedding version tracking |
| Redis | Short TTL dedup cache, rate limiting |

**Why:** Render filesystem is ephemeral; deploys wipe dedup state → duplicate candidates.

---

## 10. LLM Provider Strategy

**Current:** Anthropic Claude (`claude-sonnet-4-6`) for all AI tasks.

**Recommendation:** Stay Anthropic-primary for extraction (tool use quality is critical).

| Task | Model |
|------|-------|
| CV extraction + job fit | Claude Sonnet (current) |
| Meeting profile JSON | Claude Sonnet (current) |
| RAG answer generation | Claude Sonnet or Haiku for latency-sensitive paths |
| Embeddings | OpenAI / Voyage (see §4) |

**Cost control:** Cache extractions by CV file hash; skip re-embedding when hash unchanged.

---

## 11. Observability

**Recommendation:**
- **Sentry** for exception tracking (FastAPI integration)
- **Structured JSON logs** to Render log stream or Datadog
- **Pipeline metrics:** counters for created/updated/skipped/error (Prometheus or simple Postgres `pipeline_runs` table)

**Why:** Current logging is adequate for debugging but insufficient for production SLA awareness.

---

## 12. Testing Strategy for RAG (when built)

**Recommendation:**
- **Golden set:** 30–50 real recruiter queries with expected candidate IDs (manually labeled)
- **Metrics:** Recall@10, MRR, citation accuracy (does the answer match retrieved chunks?)
- **Regression:** Run on every PR alongside existing `pytest` suite
- **Do not** rely on LLM-as-judge alone for Hebrew retrieval quality

---

## 13. PII & Data Retention

**Recommendation:**
- Store raw CV text in vector index **encrypted at rest** (Postgres TDE or application-level for highly sensitive deployments)
- Define retention: e.g. delete chunks when Monday item is archived
- Log redaction: never log full CV text at INFO level
- Document lawful basis / consent if EU or Israeli privacy rules apply (recruitment data is sensitive)

---

## 14. Monolith vs. Split Services

**Recommendation:** **Stay monolith** until RAG search latency or team size forces a split.

**Current modular boundaries are good:**
- `services/cv_pipeline.py` — CV domain
- `crm_integration/pipeline.py` — CRM domain
- `services/monday_service.py` — shared infrastructure

**Future split candidate:** `search-service` with embedding + query API, fed by webhooks/events from the monolith.

---

## 15. Monday Webhook vs. Polling

**Current:** Event-driven webhooks for CV upload; IMAP polling for email; hybrid for Notetaker (webhook + nightly batch).

**Recommendation:** Prefer **webhooks** for anything Monday-native. Keep IMAP polling only because email is external. For RAG index freshness, hook embedding off the same events that trigger `cv_pipeline` and `crm pipeline` (single indexing function called at end of successful processing).

---

## Decision Summary Table

| Topic | Recommendation |
|-------|----------------|
| System of record | Monday.com (keep) |
| Primary AI pattern | Structured extraction (keep) + RAG for search/Q&A (add) |
| Database | PostgreSQL + pgvector |
| Embeddings | OpenAI text-embedding-3-small or Voyage multilingual |
| Search | Hybrid SQL filters + vector similarity |
| Auth | Webhook secrets now; API keys for RAG endpoints |
| Job queue | ARQ/Celery when adding embeddings |
| Dedup state | Postgres/Redis, not local files |
| Deployment | Render (keep); always-on for cron reliability |
| Service shape | Monolith with clear module boundaries |

---

## Open Questions (need product input)

1. Should unmatched CRM meetings create orphan meeting items, or continue to skip?
2. Is recruiter-facing chat in scope, or only backend automation?
3. Cross-board candidate search — Main Hub only, or all job boards?
4. Data residency requirements (Israel/EU)?
5. Acceptable latency for semantic search (sub-second vs. batch)?
