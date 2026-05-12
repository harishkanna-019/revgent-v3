# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `docs/TEAM-BRIEF.md` — team-facing brief covering architecture, cost
  projection, deployment plan, comparison with the Signal Engine
  prototype, and recommended next steps.
- `README.md` — top-level project overview, quick start, API reference,
  configuration, layout, and links to all docs.

### Removed
- `PRD.md` — superseded by `docs/ARCHITECTURE.md`, `docs/MODULES.md`,
  `docs/RULES.md`, and `docs/TEAM-BRIEF.md`. Initial requirements are
  preserved in git history; the active design lives in `docs/`.

---

## [0.10.0] - 2026-05-11

Production hardening from real Clay traffic. Two new bugs surfaced
and were fixed; cost and recall now confirmed against a live 5-company
batch.

### Fixed
- **Multi-company-digest false positives.** Articles like *"Meta to
  Cut 8,000 Jobs; Anthropic Holds AI Talks With White House"* used to
  surface as a layoff event for `anthropic.com` because the relevance
  prompt only asked "is this about {company}?". The prompt now asks
  whether `{company}` is the **actor of `{topic}`** in the article,
  with explicit examples covering multi-topic news digests. Validation
  now correctly returns 0 events for anthropic.com layoffs.
- **Wrong `primary_*` picker on Clay-flat response.** Top-level
  `primary_headline` could disagree with `answers[0].confirmation`.
  `answer_builder` now ranks `novel_fact > report > analysis >
  historical` (separate buckets) before date, and the Clay-flat
  endpoint mirrors `answers[0].valid_sources[0]` into `primary_*` so
  the top-level scalars always match the answer's confirmation source.

### Added
- Regression test `test_novel_fact_sorts_before_report` covering the
  bill.com case where a "Q1 deep dive" analysis was outranking the
  actual "cut 30% of staff" news.

---

## [0.9.0] - 2026-05-11

Standard-depth recall fix from live Clay traffic debugging.

### Changed
- `tools/topic._KEYWORD_PROMPT` now produces **generic** keywords (no
  company prefix). Cache key dropped company so batches share one LLM
  call per topic.
- `filters/stop_protocol._matches_keywords` normalizes punctuation
  (`[^a-z0-9]+ → " "`) and uses word-prefix matching so "Cost-Cutting"
  matches keyword `cost cutting` and "layoffs" matches keyword `layoff`.
- Depth timeouts bumped: cheap 30→45 s, standard 60→120 s,
  deep 120→240 s.

### Added
- `/research/clay` response diagnostics: `request_id`, `elapsed_ms`,
  `stage_trace` (per-stage `out_count`).
- `api.py` structured request/response logging using the same fields.

### Verified
Live batch against 5 companies (anthropic.com, coinbase.com, bill.com,
cloudflare.com, group1auto.com) at standard depth: 14 events + 5
signals total, $0.00258 in API fees, average 38 s per request.

---

## [0.8.0] - 2026-05-11

Cheap-depth recall fix.

### Changed
- Cheap-depth queries now use canonical brand names from
  `company.get_names()` (e.g. `"group 1 automotive layoffs"`) instead
  of URL stems (`"group1auto.com layoffs"`).

### Added
- `_CHEAP_SYNONYMS` map in `core/pipeline.py` covering layoffs, funding,
  earnings, acquisition, product launch, and leadership topics —
  50+ synonym variants total.

### Verified
5/5 test companies returned relevant results at cheap depth (previously
2/5).

---

## [0.7.0] - 2026-05-11

Topic stamping and relevance-prompt tightening.

### Fixed
- Standard/deep `tools.format.format_one` now stamps `event["topic"]`
  from `ctx.topic.original`. Previously the topic field was empty,
  causing `build_answers()` to drop every event as "No events found".
- Pipeline now also stamps the topic defence-in-depth before
  appending to `events`.

### Changed
- `tools/validate.py` relevance prompt tightened to distinguish "the
  article is about the company" from "the company is the actor of the
  topic". Reduces false positives where the company appears as a
  cited researcher, competitor, or in a multi-topic digest.

---

## [0.6.0] - 2026-05-11

Clay endpoint, optional auth, and integration guide.

### Added
- `POST /research/clay` — flat-response endpoint with top-level
  `summary`, `is_valid`, `primary_*`, `total_cost_usd`, etc. Designed
  for direct Clay column mapping.
- Optional `X-Api-Key` auth via `REVGENT_API_KEY` env var with
  `hmac.compare_digest` constant-time comparison.
- `docs/CLAY.md` — Clay column-mapping recipe.
- 6 `TestApiKeyAuth` tests covering auth required, auth correct, auth
  wrong, auth missing, auth disabled, and the GET `/` health bypass.

---

## [0.5.0] - 2026-05-11

Deployment infrastructure: Docker image, Railway config, stress
benchmarks.

### Added
- `Dockerfile` — multi-stage Python 3.11-slim, non-root user, 265 MB
  final image.
- `.dockerignore` and `railway.toml`.
- `scripts/stress.py` — in-process benchmark sampling RSS via psutil
  at 100 ms intervals.
- `scripts/stress_http.py` — HTTP benchmark sampling container RSS/CPU
  via `docker stats`.

### Verified
- Cheap depth × 10 concurrent: 22 s, 167 MB peak RSS.
- Standard depth × 20 concurrent: 46 s, 167 MB peak RSS, 14% peak CPU.
- Service is fully I/O-bound; container CPU never exceeded 15% even
  under sustained concurrency.

---

## [0.4.0] - 2026-05-11

Real OpenRouter integration; restore `:nitro` routing.

### Fixed
- **`providers/llm.py` rewritten** to use `httpx.AsyncClient` against
  OpenRouter's `/v1/chat/completions` (OpenAI-compatible) endpoint.
  Previous implementation used `anthropic.AsyncAnthropic` which only
  serves Anthropic models via `/v1/messages`. New `LLMError` and
  `LLMStatusError` exception types with status code + URL context.
- Dropped `anthropic` from `requirements.txt`.
- Added `tests/conftest.py` autouse fixture that resets provider state
  (`_client`, `_semaphore`) between tests. Fixes "Event loop is closed"
  errors on httpx clients bound to a prior pytest-asyncio loop.
- Fixed `_make_ctx(topics=[])` test helper bug.

### Changed
- Restored `:nitro` suffix on all three model identifiers in
  `core/depth.py`, `tools/company.py`, and test assertions. `:nitro`
  works on `/v1/chat/completions` and routes through OpenRouter's
  lowest-latency provider tier.

---

## [0.3.0] - 2026-05-11

External audit follow-up.

### Fixed
- Removed double-wrap in `core/runner.parallel()`.
- **Parallelized `format_one`** — pipeline `format_route` stage now
  batches event-lane candidates through `core.runner.parallel(max_workers)`
  instead of running sequentially.
- Moved env-var reads (`SEARCH_CONCURRENCY`, `SEARXNG_URL`,
  `SCRAPE_CONCURRENCY`) into `init()` so tests can override before
  provider construction.
- `api.py` accepts both `company` (v3) and `company_domain` (v2 alias)
  via `populate_by_name`.
- `answer_builder.py` now uses multi-tier confidence (high/medium/low)
  with hard-fact-first sort and URL hostname fallback for source name.
- Stripped emojis from `cli.py` (AGENTS.md rule).
- `tools/company.py` accepts `model=` parameter routed via depth policy.
- `requirements.txt` versions pinned.

---

## [0.2.0] - 2026-05-11

Initial audit fixes.

### Added
- `_MODEL_PRICING` dict in `core/depth.py` — per-model pricing for
  flash, pro, and kimi. `ctx.record(model_cost=...)` integration.
- `format.py` classification-model parameter.
- `ValueError` on unknown depth.

### Fixed
- Duplicate imports and unused imports across the codebase.
- Type annotations on public function signatures.

---

## [0.1.0] - 2026-05-11

Initial implementation. Ten vertical slices in dependency order.

### Added
- **Slice 1 - Foundation**: `models.py`, `cache.py`, `formatting.py`,
  `answer_builder.py`, `core/types.py`, `core/context.py`,
  `core/runner.py`, `core/depth.py`.
- **Slice 2 - LLM + company**: `providers/llm.py`, `tools/company.py`
  with alias resolution.
- **Slice 3 - Search + filters**: `providers/search.py`,
  `filters/dedup.py`, `filters/stop_protocol.py`, `filters/ranker.py`.
- **Slice 4 - Scrape + SSRF**: `providers/scrape.py` with
  private-IP/loopback/link-local guard.
- **Slice 5 - Topic + queries**: `tools/topic.py`, `tools/queries.py`.
- **Slice 6 - Validate + format + signals**: `tools/validate.py`,
  `tools/format.py`, `filters/signals.py`.
- **Slice 7 - Pipeline + CLI**: `core/pipeline.py` 11-stage
  orchestration, `cli.py`.
- **Slice 8 - Standard + deep depths**: per-depth model selection,
  `max_workers`, and timeouts.
- **Slice 9 - API + deployment**: `api.py` FastAPI transport with
  `POST /research`, `POST /research/async`; contract tests.
- **Slice 10 - Stress harness**: `scripts/stress.py`,
  `scripts/stress_http.py`.

### Architecture
Four-layer design: `api → core → tools → providers`. Async-first;
no `ThreadPoolExecutor` for I/O. Three semaphores (LLM 24, search 12,
scrape 8). 19 mandatory rules documented in `docs/RULES.md`.

### Tests
138 pure tests pass without API keys (sub-second). 334 pass with
`OPENROUTER_API_KEY` + `SEARXNG_URL`. Zero mocks — integration tests
hit real OpenRouter, real SearXNG, real web pages.

---

[Unreleased]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Revenanas/internal-revgent-v2/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Revenanas/internal-revgent-v2/releases/tag/v0.1.0
