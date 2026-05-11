# PRD: Revgent v3 — Async-First Research Agent

## Problem Statement

Revgent v2 is a synchronous Python application that processes corporate intelligence research requests from Clay. Under production load — dozens to hundreds of concurrent Clay row-enrichment requests — the system breaks down:

1. **Every I/O call blocks a thread.** Each LLM call (`anthropic.Anthropic.messages.create`), each SearXNG search (`requests.get`), each page scrape (`requests.get`) holds a thread hostage for 200ms–15s. Uvicorn's async event loop is nullified because every handler is sync `def`, not `async def`.

2. **Thread explosion under concurrency.** Each request spawns up to 4 nested `ThreadPoolExecutor` instances (search fan-out, scrape batch, validation, formatting) with 3–16 workers each. 50 concurrent Clay requests produce 400–800 OS threads competing for CPU, network, and file descriptors.

3. **No global backpressure on OpenRouter.** Each request independently fires 10–20 LLM calls in parallel. 50 concurrent requests means 500–1000 near-simultaneous OpenRouter calls — guaranteed rate limit storms, wasted retries, and cascading 5/10/15-second `time.sleep()` blocks that burn threads doing nothing.

4. **Thread-unsafe shared state.** Module-level `TTLCache` instances (keyword cache, company name cache, SearXNG cache), SearXNG's `requests.Session`, and the circuit breaker dict are all read/written concurrently from `ThreadPoolExecutor` workers with no locking. This produces cache corruption, lost writes, and stale circuit breaker state.

5. **Dead provider weight.** Firecrawl, Tavily, and Cloudflare Browser Run adapters add ~400 lines of code, two SDK dependencies (`firecrawl`, `tavily-python`), and a three-level fallback chain in `search_router.py` — none of which are used in production. The only providers are OpenRouter (LLM), SearXNG (search), and trafilatura (scrape).

6. **Orchestration code bloat.** The pipeline is spread across `research_service.py` (241 lines), `pipeline_runner.py` (94 lines), `pipeline_stages.py` (160 lines), and `execution_context.py` (125 lines) — 620 lines of orchestration for a 10-stage pipeline. A separate 65-line fast path (`_research_company_fast`) duplicates search/filter/rank logic for cheap mode.

The system works for single requests. It does not work when called like a rocket.

## Solution

Rewrite Revgent as a fully async Python application with three external providers (OpenRouter, SearXNG, trafilatura), global concurrency control via `asyncio.Semaphore`, and zero `ThreadPoolExecutor` usage. The entire codebase shrinks from ~3,300 lines to ~1,600 lines while preserving every feature, the API contract, the depth system, budget enforcement, cost attribution, and the signal routing lane.

The architecture follows the patterns proven in `pi-agent-core`:
- **Provider boundary** — three async adapters behind simple `async def` interfaces, each with its own semaphore
- **Tool functions** — async functions that compose providers with prompts, returning a uniform `ToolResult`
- **Pipeline loop** — one orchestration function that reads top-to-bottom as a stage list, with budget checks between stages and event emission at boundaries
- **Parallel runner** — one 20-line function (`asyncio.gather` + `Semaphore`) replacing all `ThreadPoolExecutor` usage
- **Filter functions** — pure sync functions with no I/O, testable with no infrastructure

Every capability that exists today must either work or raise an explicit error with actionable context. No silent no-ops, no swallowed exceptions, no functions that return empty strings when they should tell you what failed.

## User Stories

1. As a Clay table operator, I want Revgent to handle 100+ concurrent cheap-depth requests without degrading response time, so that batch enrichment of large company lists completes in minutes not hours.
2. As a Clay table operator, I want each cheap-depth request to complete in under 5 seconds, so that per-row enrichment feels instant.
3. As a Clay table operator, I want Revgent to never exceed my per-request budget even under concurrent load, so that batch runs have predictable cost.
4. As a sales researcher, I want standard-depth research to return within 15 seconds, so that I can use it in real-time during prospect preparation.
5. As a sales researcher, I want deep-depth research to use the strongest available models and extract full article content, so that high-value deal research is thorough.
6. As an API consumer, I want the `/research` response shape to be identical to v2, so that existing Clay integrations work without modification.
7. As an API consumer, I want the `/research/async` webhook endpoint to work without spawning OS threads, so that the server can handle many concurrent async requests.
8. As an API consumer, I want structured error responses when my request is invalid (bad depth, budget exceeds absolute max, missing company domain), so that I can fix the request programmatically.
9. As an operator, I want a single environment variable (`LLM_CONCURRENCY`) to cap the total in-flight OpenRouter calls across all concurrent requests, so that I can tune throughput without code changes.
10. As an operator, I want a single environment variable (`SEARCH_CONCURRENCY`) to cap concurrent SearXNG calls, so that I can protect my self-hosted SearXNG instance from overload.
11. As an operator, I want a single environment variable (`SCRAPE_CONCURRENCY`) to cap concurrent page scrape calls, so that I can control outbound HTTP load.
12. As an operator, I want the circuit breaker on SearXNG to fail fast (2 consecutive failures, 30-second cooldown) and raise an explicit error instead of silently returning empty results, so that I know when my search infrastructure is down.
13. As an operator, I want LLM retry backoff to use non-blocking async sleep instead of `time.sleep()`, so that retries don't waste threads.
14. As an operator, I want the keyword cache, company name cache, and search cache to be safe for concurrent async access, so that cache corruption doesn't cause duplicate LLM calls or stale results.
15. As an operator, I want zero `ThreadPoolExecutor` usage in the codebase, so that concurrency is controlled exclusively through asyncio semaphores and the system's thread count stays constant under load.
16. As a developer, I want the pipeline to read as a top-to-bottom stage list in one function, so that I can understand the entire research flow without jumping between 4 files.
17. As a developer, I want each tool (topic analyzer, query generator, validator, formatter, company extractor) to be a single async function with a `ToolResult` return type, so that adding or modifying a pipeline stage is one file change.
18. As a developer, I want filters (stop protocol, dedup, ranker, signal routing) to be pure functions with no I/O, so that they're testable with a function call and no infrastructure.
19. As a developer, I want the cheap depth path to use the same pipeline function as standard/deep with different tool behavior (regex keywords instead of LLM, no scraping, no summarization), not a separate duplicated function.
20. As a developer, I want every provider adapter to raise explicit errors with context (the URL that failed, the model that timed out, the HTTP status code) instead of returning empty strings or empty lists, so that debugging production issues doesn't require adding print statements.
21. As a developer, I want the scraper to validate every URL before fetching (SSRF protection: no private IPs, no loopback, no link-local, check every DNS answer) and raise a clear error if a URL is blocked, so that the security boundary is visible and testable.
22. As a developer, I want trafilatura extraction to run in `asyncio.run_in_executor` since it's CPU-bound, so that HTML parsing doesn't block the event loop.
23. As a developer, I want the parallel runner to preserve source order (results returned in input order, not completion order), so that pipeline output is deterministic.
24. As a developer, I want pipeline events (stage start/end, item validation results, budget warnings) emitted via a callback, so that the API layer can stream progress to webhooks and the CLI can render a progress display.
25. As a developer, I want the `requirements.txt` to contain only the dependencies actually used (anthropic, httpx, python-dotenv, fastapi, uvicorn, trafilatura, pytest, pytest-asyncio), with Firecrawl, Tavily, and requests removed.
26. As a developer, I want the research depth policy (cheap/standard/deep caps, model routing, worker counts) to remain a frozen dataclass with a `model_for_task()` method, so that depth configuration is centralized and immutable per-request.
27. As a developer, I want cost attribution (shared costs amortized across items, direct costs tagged by URL) to work identically to v2, so that Clay can show per-row enrichment cost.
28. As a developer, I want the answer builder (validated answer objects with validity, confirmation, timing, summary, sources) to work identically to v2, so that Clay's structured answer format is preserved.
29. As a developer, I want the signal routing lane (opinion/speculation routed to signals with type classification and confidence scores) to work identically to v2, so that soft intelligence is preserved alongside hard facts.
30. As a developer, I want tests to run against real infrastructure (real SearXNG, real OpenRouter, real trafilatura) not mocks, so that test results reflect production behavior.
31. As a developer, I want tests for the pure filter functions (stop protocol, dedup, ranker, signals) to run without any infrastructure at all, so that they execute in milliseconds.
32. As a developer, I want a single `asyncio.run()` CLI entry point for local testing, so that I can run the pipeline from the command line without starting a server.

## Implementation Decisions

### Architecture

The system has four layers. Each layer depends only on the layer below it.

```
Transport     api.py (FastAPI, fully async def)
Pipeline      core/pipeline.py (one function: run())
Tools         tools/*.py (async functions composing providers + prompts)
Providers     providers/llm.py, providers/search.py, providers/scrape.py
```

Pure logic lives outside all layers:
- `filters/` — stop protocol, dedup, ranker, signals (sync, no I/O)
- `models.py` — UsageStats, CostTracker (data only)
- `formatting.py` — parse_date, format_event (pure transforms)
- `answer_builder.py` — build_answers (pure transforms)

### Three Providers, Three Semaphores

Each provider adapter owns one `asyncio.Semaphore` gating its external I/O:

- **LLM provider** (`providers/llm.py`): `AsyncAnthropic` (httpx-based). Semaphore default 24 (`LLM_CONCURRENCY` env var). One shared httpx connection pool. Retry with `asyncio.sleep()` on 500/529/rate/overloaded errors (2s, 4s, 6s backoff). Reasoning model token floor enforcement with empty-text retry. Raises `RuntimeError` after exhausting retries — never returns empty string silently.

- **Search provider** (`providers/search.py`): `httpx.AsyncClient`. Semaphore default 12 (`SEARCH_CONCURRENCY` env var). Circuit breaker: 2 consecutive failures trips the circuit for 30 seconds. Short TTL cache (60s) keyed by `(query, max_days, limit)`. Date parsing from SearXNG metadata (relative "N days ago", absolute DD/MM/YYYY, ISO). When the circuit is open, raises an explicit error — does not silently return empty results.

- **Scrape provider** (`providers/scrape.py`): `httpx.AsyncClient`. Semaphore default 8 (`SCRAPE_CONCURRENCY` env var). SSRF protection: validate every URL before fetch (scheme whitelist, DNS resolution, every IP must be public-routable, redirect following with per-hop validation). HTML fetch via httpx, content extraction via `trafilatura.extract()` in `asyncio.run_in_executor()` (CPU-bound). Article quality gate (minimum 80 chars, reject error page markers). Raises clear error on SSRF block — does not silently return empty string.

Each provider exposes `async def init()` and `async def close()`, called from FastAPI's `lifespan` context manager. This initializes clients and semaphores inside the running event loop (avoiding lazy-init races). Imports work without API keys. Missing keys raise `ValueError` with an actionable message on `init()` or first call.

### Parallel Runner

One function: `parallel(fn, items, max_workers) → list[results]`. Uses `asyncio.gather(*tasks, return_exceptions=True)` with a local `asyncio.Semaphore(max_workers)`. Results are returned in input order (guaranteed by `asyncio.gather`). Exception results are returned as values, not raised — callers inspect each result and handle errors per-item. This is the same error isolation pattern as pi-agent-core's `executeToolCallsParallel`. Replaces all 6 `ThreadPoolExecutor` usages in v2.

### Pipeline

One async function: `run(ctx, emit) → response_dict`. The pipeline reads top-to-bottom as a stage list. Cheap mode is handled by conditional branches within the same function, not a separate code path. Budget enforcement happens between stages via `ctx.exhausted` checks — `break` from the topic loop, assemble partial response.

The `emit` callback receives typed event dataclasses (StageStart, StageEnd, ItemResult, BudgetCheck). The API layer can subscribe for progress streaming. If `emit` is None, events are discarded.

### Tools

Each tool is a standalone async function that takes `RunContext` and input, returns `ToolResult(output, usage, item_id)`. Tools compose provider calls with prompts. They do not own retry logic (the provider handles that) or parallelism (the pipeline calls `parallel()` when needed).

- `tools/topic.py` — `analyze(ctx) → ToolResult`: simplify topic (skip if ≤3 words) + generate keywords (cached). 2 LLM calls max.
- `tools/queries.py` — `generate(ctx) → ToolResult`: generate search queries. 1 LLM call.
- `tools/validate.py` — `validate_one(ctx, candidate) → ToolResult`: company relevance check (with step-by-step retry on UNCERTAIN) + fact check. 2–3 LLM calls.
- `tools/format.py` — `format_one(ctx, candidate) → ToolResult`: summarize + classify fired concurrently via `asyncio.create_task`. 2 LLM calls, concurrent.
- `tools/company.py` — `get_names(company_domain) → ToolResult`: company name variations. 1 LLM call, cached 24h.

### Run Context

One `RunContext` instance per pipeline invocation. Never shared across requests. Contains:
- Immutable: policy, company_domain, topics, date_min, date_max
- Mutable accumulators: UsageStats, CostTracker, events list, signals list
- Current topic state: TopicState dataclass (original, simplified, keywords, queries)

Uses `__slots__` for minimal memory footprint under high concurrency.

### Cache

An `AsyncTTLCache` class with lock-free reads and `asyncio.Lock`-protected writes. Provides `get_or_compute(key, async_fn)` with per-key locking to prevent thundering herd — if 100 requests miss the cache for the same company, only one calls the LLM. Used by keyword cache (24h TTL) and company name cache (24h TTL). Search cache (60s TTL) lives inside the search provider as a plain dict.

### Error Philosophy

Every function that can fail must either succeed or raise an exception with actionable context. Specific rules:
- Provider calls raise on exhausted retries (LLM), open circuit (search), or SSRF block (scrape)
- The pipeline catches provider errors at stage boundaries and decides whether to continue with partial results or propagate
- Budget exhaustion is not an error — it's a normal exit condition that produces a partial response
- Missing API keys raise immediately on first call, not silently return empty results
- Invalid request parameters (bad depth, budget exceeds max) raise `ValueError` at the API layer

### API Contract

Response shape is identical to v2. Same Pydantic models, same field names, same defaults. The `/research` endpoint becomes `async def`. The `/research/async` endpoint uses `asyncio.create_task()` instead of `threading.Thread`.

### Deployment

Same Railway Procfile: `web: uvicorn api:app --host 0.0.0.0 --port $PORT`. Single worker, single event loop. The async architecture handles concurrency within the process — no need for multiple uvicorn workers. `--timeout-graceful-shutdown 30` for clean shutdown with in-flight request draining.

### Lifecycle Management

Provider clients and semaphores are initialized in FastAPI's `lifespan` event (not lazy-init). HTTP clients are closed on shutdown. Background webhook tasks are tracked and awaited during shutdown. Optional pipeline-level timeout via `asyncio.wait_for()` prevents runaway deep-depth requests.

### Dependencies

Only what's used:
- `anthropic` — AsyncAnthropic for OpenRouter
- `httpx` — async HTTP client (already an anthropic transitive dep)
- `python-dotenv` — env var loading
- `fastapi` + `uvicorn` — API server
- `trafilatura` — HTML content extraction
- `pytest` + `pytest-asyncio` — testing

Removed: `tavily-python`, `requests`, `firecrawl`.

## Testing Decisions

### Testing Philosophy

Tests run against real infrastructure. No mocks. No `unittest.mock.patch`. No module-level monkeypatching.

A good test:
- Calls the public interface of a module with real inputs and asserts on real outputs
- For provider tests: hits the real SearXNG instance, makes real OpenRouter calls, fetches real web pages
- For filter tests: passes real data structures and asserts on real return values
- For pipeline tests: runs the full pipeline against real providers and asserts on response shape
- Is deterministic where possible (filters, formatting, ranking) and asserts on structural properties where not (LLM output contains expected keywords, search returns non-empty results for known queries)
- Runs fast for pure logic (milliseconds), accepts latency for integration tests (seconds)

### Modules Under Test

**Provider tests** — real calls:
- LLM provider: call with a real model, assert text is non-empty and usage dict has expected keys. Test retry behavior with a model that intermittently errors. Test semaphore bounds concurrent calls.
- Search provider: query SearXNG with a known term, assert results have title/url/content/published_date. Test cache returns same results on second call. Test circuit breaker trips after consecutive failures.
- Scrape provider: scrape a known news article URL, assert extracted text is non-empty and passes the article quality gate. Test SSRF rejection for localhost/private IPs. Test `scrape_many` returns results in input order.

**Filter tests** — pure logic, no infrastructure:
- Stop protocol: date window filtering, source credibility (excluded domains), topic relevance (keyword matching), company relevance. Carry forward from v2's 18 existing stop protocol tests.
- Dedup: duplicate URL removal preserving first occurrence order.
- Ranker: metadata scoring produces expected ordering for known inputs. Carry forward from v2's 9 ranker tests.
- Signals: lane routing (valid→event, opinion→signal, invalid→discard) with correct signal types and confidence scores.

**Pipeline tests** — real end-to-end:
- Cheap depth: full pipeline for a known company + topic, assert response has events, usage, cost, budget fields. Assert budget is not exceeded.
- Standard depth: full pipeline, assert events have summaries (LLM-generated) and content types.
- Budget enforcement: run with a very low budget, assert partial results and `budget.exhausted = true`.
- Empty results: query for a topic with no news, assert empty events list and topic_results.topic_found = false.

**Runner tests** — real async:
- Source order preservation: run parallel with items that complete in random order, assert output order matches input order.
- Semaphore enforcement: run more items than max_workers, assert no more than max_workers execute concurrently.

**Model tests** — pure logic:
- UsageStats: add + to_dict.
- CostTracker: record, budget enforcement, per-item attribution, shared cost calculation.
- AsyncTTLCache: get/set, expiration, get_or_compute.

**API contract tests** — real server:
- `/research` returns valid ResearchResponse shape.
- `/research/async` returns processing status immediately.
- Health check returns `{"status": "ok"}`.

**Prior art:** v2 has 244+ tests across 27 test files, running in ~0.5s with mocked providers. v3 integration tests will be slower (real network calls) but more trustworthy. Pure logic tests (filters, models, formatting) should remain sub-second.

## Out of Scope

- **Firecrawl support.** Not used in production. Deleted entirely.
- **Tavily support.** Not used in production. Deleted entirely.
- **Cloudflare Browser Run fallback.** Not used in production. Deleted entirely.
- **The search router fallback chain.** SearXNG is the only search provider. No fallback to Firecrawl or Tavily. If SearXNG is down, the circuit breaker trips and the request gets an error.
- **The `src/main.py` deprecated compatibility shim.** v3 is a clean break. No backward compatibility with v2's `src.main.research_company()`.
- **LLM-driven tool selection.** The pipeline is orchestrated (stages run in fixed order), not autonomous (LLM doesn't choose which tools to call). This is a deliberate choice for cost predictability and deterministic behavior under Clay batch load.
- **Streaming responses.** The `/research` endpoint returns a complete JSON response. Server-sent events or streaming chunks are not in scope.
- **Multi-worker uvicorn deployment.** The async architecture handles concurrency within a single process. Multi-worker scaling is a deployment concern, not an application concern.
- **WebSocket transport.** HTTP POST only.
- **Authentication/authorization.** Not in scope for v3. The API is called from Clay's internal network.
- **Rate limiting at the API layer.** Backpressure is handled at the provider layer via semaphores. API-level rate limiting (per-client, per-IP) is not in scope.

## Further Notes

### Concurrency Model

Under 100 concurrent Clay cheap-depth requests:
- **Current v2:** ~100 sync threads in uvicorn, each spawning 3–16 more via ThreadPoolExecutor = 400–800 OS threads. No global LLM backpressure. Race conditions in every cache.
- **v3:** 100 coroutines on 1 event loop (~80KB total, measured ~800 bytes per task+coroutine). Max 24 LLM calls in-flight (semaphore). Max 12 SearXNG calls in-flight. Max 8 scrape calls in-flight. All caches async-safe. Zero threads for I/O (trafilatura gets executor threads for CPU-bound extraction at ~4ms per page).

### Line Count

| | v2 | v3 | Delta |
|---|---|---|---|
| Source | ~3,300 lines, 30 files | ~1,600 lines, 20 files | -51% |
| Dead providers | ~400 lines (Firecrawl, Tavily, Cloudflare, search_router) | 0 | -100% |
| Orchestration | ~620 lines (4 files) | ~170 lines (pipeline + runner) | -73% |
| Dependencies | 8 (including 2 unused SDKs) | 6 | -2 |

### Migration

v3 is a clean rewrite in a new directory, not an incremental refactor of v2. The API contract is identical so Clay integrations work without changes. The deployment target (Railway, same Procfile) is unchanged. v2 stays running until v3 is validated in production.

### Critical Invariants to Preserve

1. Budget enforcement: a request with `max_cost=0.01` must never spend more than $0.01 in LLM calls, regardless of concurrency
2. Cost attribution: `sum(event.cost_attribution for event in events + signals) ≈ cost.total_cost`
3. Response shape: every field in the v2 ResearchResponse Pydantic model must exist with the same type in v3
4. Depth behavior: cheap mode uses regex keywords + no scraping + no summarization; standard uses LLM everything; deep uses stronger models + more candidates + full extraction
5. Source order: parallel tool execution returns results in input order, so pipeline output is deterministic
6. SSRF protection: no HTTP request to a private/loopback/link-local IP address, even via DNS rebinding or redirect chain
