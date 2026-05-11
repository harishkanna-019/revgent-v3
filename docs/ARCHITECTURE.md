# Architecture

## System Overview

Revgent v3 is a corporate intelligence research agent. It takes a company domain and topics, searches the web, validates results with LLMs, and returns verified news events with AI summaries.

Three external providers. One event loop. Zero threads for I/O.

```
┌──────────────────────────────────────────────────────────────┐
│                     Clay / API Consumer                      │
│                  POST /research { domain, topics, depth }    │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                      Transport Layer                         │
│                                                              │
│   api.py — FastAPI, fully async def handlers                 │
│   Pydantic request/response validation                       │
│   Error → HTTPException mapping                              │
└──────────────────────────┬───────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────┐
│                     Pipeline Layer                            │
│                                                              │
│   core/pipeline.py — run(ctx, emit) → response_dict          │
│   One function, top-to-bottom stage list                     │
│   Budget enforcement between stages                          │
│   Event emission at boundaries                               │
│                                                              │
│   core/runner.py — parallel(fn, items, max_workers)          │
│   asyncio.gather + Semaphore, source-order results           │
│                                                              │
│   core/context.py — RunContext (per-request mutable state)   │
│   core/depth.py — ResearchDepthPolicy (frozen, immutable)    │
│   core/types.py — ToolResult, Event types                    │
└───────────┬──────────────────────────────────┬───────────────┘
            │                                  │
┌───────────▼───────────┐    ┌─────────────────▼───────────────┐
│      Tool Layer       │    │          Filter Layer            │
│                       │    │                                  │
│  tools/topic.py       │    │  filters/stop_protocol.py       │
│  tools/queries.py     │    │  filters/dedup.py               │
│  tools/validate.py    │    │  filters/ranker.py              │
│  tools/format.py      │    │  filters/signals.py             │
│  tools/company.py     │    │                                  │
│                       │    │  Pure functions. Sync. No I/O.   │
│  Async functions.     │    │  No provider calls.              │
│  Compose providers    │    │                                  │
│  with prompts.        │    └──────────────────────────────────┘
└───────────┬───────────┘
            │
┌───────────▼───────────────────────────────────────────────────┐
│                      Provider Layer                           │
│                                                               │
│  ┌─────────────────┐  ┌──────────────────┐  ┌──────────────┐ │
│  │ providers/llm.py│  │providers/search.py│  │providers/    │ │
│  │                 │  │                   │  │  scrape.py   │ │
│  │ AsyncAnthropic  │  │ httpx.AsyncClient │  │ httpx +      │ │
│  │ → OpenRouter    │  │ → SearXNG         │  │ trafilatura  │ │
│  │                 │  │                   │  │              │ │
│  │ Semaphore(24)   │  │ Semaphore(12)     │  │ Semaphore(8) │ │
│  │ Retry + backoff │  │ Circuit breaker   │  │ SSRF guard   │ │
│  │ Reasoning floors│  │ 60s TTL cache     │  │ Quality gate │ │
│  └────────┬────────┘  └────────┬──────────┘  └──────┬───────┘ │
│           │                    │                     │         │
└───────────┼────────────────────┼─────────────────────┼─────────┘
            │                    │                     │
            ▼                    ▼                     ▼
      OpenRouter API     Self-hosted SearXNG    Public web pages
```

## Dependency Rules

Strict downward-only. No layer imports from a layer above it.

```
api.py          → core/*
core/pipeline   → tools/*, filters/*, core/runner, core/context
tools/*         → providers/*
filters/*       → (nothing external — pure logic)
providers/*     → (external services only)
models.py       → (nothing — data only)
formatting.py   → (nothing — pure transforms)
```

Violations of this rule indicate an architectural problem.

## Concurrency Model

### One Event Loop

Uvicorn runs one async event loop in one OS process. Every request is a coroutine on that loop. All I/O is `await`-based. The loop never blocks.

### Three Semaphores

Each provider owns one `asyncio.Semaphore`. These are the ONLY concurrency controls in the system. No `ThreadPoolExecutor`, no `threading.Lock`, no `multiprocessing`.

```
┌─────────────────────────────────────────────────────────┐
│                 uvicorn event loop                       │
│                                                         │
│  Request 1 ──┐                                          │
│  Request 2 ──┼─── coroutines ───┐                       │
│  Request 3 ──┤                  │                       │
│  ...         │                  ▼                       │
│  Request N ──┘                                          │
│                    ┌──────────────────────┐              │
│                    │ LLM_SEMAPHORE(24)    │              │
│                    │ All LLM calls queue  │              │
│                    │ here. Max 24 active. │              │
│                    └──────────────────────┘              │
│                    ┌──────────────────────┐              │
│                    │ SEARCH_SEMAPHORE(12) │              │
│                    │ All SearXNG calls    │              │
│                    │ queue here.          │              │
│                    └──────────────────────┘              │
│                    ┌──────────────────────┐              │
│                    │ SCRAPE_SEMAPHORE(8)  │              │
│                    │ All page fetches     │              │
│                    │ queue here.          │              │
│                    └──────────────────────┘              │
│                                                         │
│  trafilatura CPU work → asyncio default executor        │
│  (only non-async I/O in the system)                     │
└─────────────────────────────────────────────────────────┘
```

Under 100 concurrent requests:
- 100 coroutines, ~80KB total memory (~800 bytes per task+coroutine, measured with tracemalloc). Not 100 threads at 8MB stack each on macOS.
- Max 24 LLM calls in-flight regardless of request count
- Max 12 SearXNG queries in-flight
- Max 8 page scrapes in-flight
- Semaphores queue excess work transparently — no rate limit storms

### Why Not Threads

| | ThreadPoolExecutor (v2) | asyncio.Semaphore (v3) |
|---|---|---|
| 50 requests × 10 LLM calls each | 500 threads competing | 24 in-flight, 476 queued |
| Memory per task | ~8MB (thread stack, macOS) | ~800 bytes (coroutine + task, measured) |
| Retry backoff | `time.sleep(5)` blocks thread | `asyncio.sleep(2)` yields loop |
| Cache access | race conditions | single-threaded event loop |
| Connection pools | one per ThreadPool | one per provider, shared |

## Data Flow

### Per-Request Lifecycle

```
POST /research
  │
  ├─ Validate request (Pydantic)
  ├─ Create ResearchDepthPolicy (frozen, immutable)
  ├─ Create RunContext (mutable, request-scoped)
  │
  ├─ [standard/deep] Resolve company names once (tools/company, cached 24h)
  │
  ├─ For each topic:
  │   │
  │   ├─ [cheap] Regex keywords + 2 hardcoded queries
  │   ├─ [standard/deep] LLM simplify → LLM keywords → LLM queries
  │   │
  │   ├─ Search fan-out (N concurrent SearXNG queries, per-query error isolation)
  │   ├─ Dedup by URL
  │   ├─ Stop protocol (date → credibility → topic → company names)
  │   ├─ [cheap] Fast company mention check (regex, no LLM)
  │   ├─ Rank by metadata score
  │   │
  │   ├─ [standard/deep] Scrape top N pages (concurrent, per-page error isolation)
  │   │
  │   ├─ Validate + fact-check (parallel, per-item error isolation)
  │   │   ├─ valid → validated list
  │   │   ├─ opinion → signals list
  │   │   ├─ invalid → discarded
  │   │   └─ error → discarded (logged, does not kill other candidates)
  │   │
  │   ├─ [standard/deep] Summarize + classify (parallel, per-item error isolation)
  │   ├─ [cheap] Format from snippet (no LLM summary)
  │   │
  │   └─ Budget check → break if exhausted
  │
  ├─ Cost attribution (shared + direct per item)
  ├─ Build answers (per-topic validity/confirmation/timing)
  └─ Return response
```

**Multi-topic response assembly:** `topic_results.topic_name` is the simplified name of the last topic if single-topic, or a comma-joined string of all original topics if multi-topic. `build_answers()` receives the full topic list to produce one answer per requested topic.

### Budget Enforcement

Budget checks happen between stages, not inside them. A stage that starts will complete — budget exhaustion causes the pipeline to skip remaining topics, not abort mid-LLM-call.

```
Stage 1 (topic) → check → Stage 2 (queries) → check → Stage 3 (search) → ...
                    │                            │
                    └─ if exhausted: break ───────┘
```

This means a request with `max_cost=0.01` might spend $0.012 if the last LLM call pushed it over — but it will never start another stage after exceeding the budget.

### Cost Attribution

Every `ToolResult` carries a `usage` dict and optional `item_id`. The pipeline calls `ctx.record(usage, item_id)` after each tool execution. This feeds into `CostTracker` which maintains:

- **Per-item costs:** tagged by URL (validation, fact-check, summary, classification, extraction)
- **Shared costs:** no item_id (topic simplification, keyword gen, query gen)

At response assembly, shared costs are amortized evenly across all items. Each event/signal gets `cost_attribution = shared_portion + direct_cost`.

## Provider Details

### OpenRouter LLM

- **Client:** `anthropic.AsyncAnthropic` with `base_url="https://openrouter.ai/api"`
- **Connection pool:** one `httpx.AsyncClient` shared across all requests (managed by anthropic SDK internally via `self._client`). Pool limits: 1000 max connections, 100 keepalive. The LLM semaphore (24) is the actual concurrency bottleneck, not the connection pool.
- **Semaphore:** `LLM_CONCURRENCY` env var, default 24
- **Retry:** 3 attempts. Retries on 500, 529, rate limit, overloaded. Backoff: 2s, 4s, 6s via `asyncio.sleep()`
- **Reasoning models:** prefixes `deepseek/`, `moonshotai/kimi-`. Token floor enforcement (256/1024). Empty-text retry with doubled max_tokens up to 4096. Reasoning effort set to `minimal` with `exclude: True` to keep output cheap.
- **Model routing:** `:nitro` variants for throughput. Cheap/standard use `deepseek-v4-flash:nitro`. Deep uses `deepseek-v4-pro:nitro` and `kimi-k2.6:nitro`.
- **Failure mode:** raises `RuntimeError` after exhausting retries. Never returns empty text silently.

### SearXNG Search

- **Client:** `httpx.AsyncClient`, single instance, shared across requests
- **Semaphore:** `SEARCH_CONCURRENCY` env var, default 12
- **Endpoint:** `GET {SEARXNG_URL}/search?format=json&categories=news`
- **Timeout:** 15 seconds per query
- **Cache:** in-memory dict keyed by `(query, max_days, limit)`, 60-second TTL
- **Circuit breaker:** 2 consecutive failures → open for 30 seconds. While open, raises immediately.
- **Date parsing:** SearXNG returns dates in `publishedDate` or `metadata` fields. Parser handles ISO, "N days ago", "N hours ago", DD/MM/YYYY, YYYY-MM-DD.
- **Time range mapping:** `max_days` → SearXNG `time_range` param (≤1→day, ≤7→week, ≤31→month, ≤365→year)
- **Failure mode:** raises `SearchCircuitOpen` when circuit is tripped. Individual query network errors are caught internally — the query returns `[]` and the circuit breaker records the failure. `search_many()` isolates per-query errors so one failed query does not abort the fan-out.

### trafilatura Scrape

- **HTTP client:** `httpx.AsyncClient`, single instance, shared across requests
- **Semaphore:** `SCRAPE_CONCURRENCY` env var, default 8
- **Timeout:** 10 seconds per page
- **SSRF protection:**
  - URL scheme must be `http` or `https`
  - Hostname must resolve to public-routable IPs
  - Every DNS answer checked (not just first)
  - Private, loopback, link-local, multicast, reserved, unspecified IPs blocked
  - Redirects followed manually (max 5 hops), each hop validated
  - DNS resolution results cached with `@lru_cache(maxsize=2048)`
- **Extraction:** `trafilatura.extract()` runs in `asyncio.run_in_executor(None, ...)` (CPU-bound, measured ~4ms per page). Default executor size is `min(32, cpu_count + 4)` which is always larger than the scrape semaphore (8), so executor capacity is never the bottleneck.
- **Quality gate:** minimum 80 chars, reject error page markers ("404 error", "page not found", "access denied", "enable javascript", "please enable cookies")
- **Failure mode:** raises `SSRFBlocked` on SSRF rejection. Returns empty string when the page loads but trafilatura extraction produces nothing useful (quality gate — not an error). Raises `ScrapeError` on network timeout or HTTP 5xx. The pipeline wraps scrape calls with per-item error isolation so one failed page does not abort scraping of other pages.

## Depth Profiles

| | Cheap | Standard | Deep |
|---|---|---|---|
| **Use case** | Clay row enrichment | Dashboard research | Deep investigation |
| **Budget** | $0.01 | $0.50 | $2.00 |
| **Queries/topic** | 2 (hardcoded) | 8 (LLM-generated) | 12 (LLM-generated) |
| **Candidates/topic** | 3 | 10 | 20 |
| **Full extraction** | 0 (snippets only) | 5 pages | All |
| **Workers** | 3 | 8 | 16 |
| **Topic analysis** | Regex split | LLM simplify + LLM keywords | LLM (stronger models) |
| **Validation** | LLM (flash) for top 3 | LLM (flash) | LLM (kimi-k2.6) |
| **Summarization** | None (snippet as description) | LLM (flash) | LLM (kimi-k2.6) |
| **Wall time (est.)** | 2–5s | 8–15s | 15–30s |

All three modes use the same pipeline function with conditional branches. No separate code path for cheap mode.

## Response Shape

```json
{
  "company": "meta.com",
  "events": [
    {
      "headline": "Meta lays off 1,500 employees...",
      "description": "AI-generated summary...",
      "topic": "layoffs",
      "date": "2026-01-16",
      "source_name": "reuters.com",
      "source_url": "https://...",
      "content_type": "novel_fact",
      "headline_has_numbers": true,
      "cost_attribution": 0.00034
    }
  ],
  "answers": [
    {
      "topic": "layoffs",
      "validity": { "is_valid": true, "statement": "...", "confidence": "high" },
      "confirmation": { "is_confirmed": true, "statement": "...", "source_name": "...", "source_url": "..." },
      "timing": { "happened_at": "2026-01-16", "statement": "..." },
      "summary": "...",
      "valid_sources": [{ "title": "...", "source_name": "...", "source_url": "...", "published_date": "...", "supports_claim": true }]
    }
  ],
  "signals": [
    {
      "headline": "...",
      "description": "...",
      "topic": "...",
      "date": "...",
      "source_name": "...",
      "source_url": "...",
      "signal_type": "market_speculation",
      "confidence": 0.35,
      "why_not_event": "Relevant to the company, but failed the hard-fact check.",
      "cost_attribution": 0.00012
    }
  ],
  "usage": { "input_tokens": 10545, "output_tokens": 1235, "total_tokens": 11780 },
  "topic_results": { "topic_found": true, "topic_count": 1, "topic_name": "layoffs" },
  "cost": { "total_cost": 0.0023, "budget": 0.50, "budget_exhausted": false, "breakdown": { "llm": 0.0023 } },
  "budget": { "requested": 0.50, "remaining": 0.4977, "exhausted": false }
}
```

Identical to v2. No field changes.

## Error Isolation Model

The system has three error isolation boundaries, inspired by pi-agent-core's `executePreparedToolCall` which catches per-tool errors:

### Per-Item Isolation (parallel stages)

When the pipeline runs validation, formatting, or scraping in parallel over N items, each item's errors are caught individually. One candidate that causes an LLM timeout does not cancel the other 9 candidates.

The `parallel()` runner uses `asyncio.gather(return_exceptions=True)`. The pipeline inspects each result: if it's an exception, the item is discarded (logged via `emit(ItemResult(..., status="error"))`). If it's a `ToolResult`, it's processed normally.

### Per-Query Isolation (search fan-out)

`search_many()` catches per-query errors internally. If 2 of 8 SearXNG queries fail, the pipeline gets results from the 6 that succeeded. Failed queries return empty lists — the circuit breaker still records the failure.

### Per-Stage Isolation (pipeline level)

If an entire stage fails (e.g., SearXNG circuit is open for all queries), the pipeline catches at the stage boundary. For search: zero results means zero candidates, the topic produces no events but the pipeline continues to the next topic. For LLM provider failure: if the semaphore-gated provider is completely down, the pipeline produces a partial response with whatever it gathered before the failure.

Budget exhaustion is not in the error path — it's a normal control flow exit.

## Lifecycle Management

### Provider Client Initialization

Module-level provider clients (`AsyncAnthropic`, `httpx.AsyncClient`) and semaphores (`asyncio.Semaphore`) are initialized on first use via a module-level init function called from a FastAPI `lifespan` event. This avoids the lazy-init race where two coroutines both create the singleton simultaneously.

```
@asynccontextmanager
async def lifespan(app):
    # Initialize all providers (creates clients + semaphores in the running loop)
    await providers.llm.init()
    await providers.search.init()
    await providers.scrape.init()
    yield
    # Shutdown: close HTTP clients, release connection pools
    await providers.search.close()
    await providers.scrape.close()
```

### Graceful Shutdown

On SIGTERM, uvicorn drains in-flight requests (configurable `--timeout-graceful-shutdown`). The lifespan `yield` block closes httpx clients, which closes TCP connection pools cleanly. Background webhook tasks (`asyncio.create_task`) are tracked in a set and awaited during shutdown.

### Default Executor Sizing

`trafilatura.extract()` runs in `asyncio.run_in_executor(None, ...)` which uses the default `ThreadPoolExecutor`. The default size is `min(32, os.cpu_count() + 4)` (e.g., 15 on an 11-core machine). Trafilatura extraction is measured at ~4ms per page. The scrape semaphore (default 8) limits in-flight extractions, so the executor always has spare capacity.

## Pipeline Timeout

An optional `timeout` parameter on `pipeline.run()` wraps the entire execution in `asyncio.wait_for()`. If the timeout fires, in-flight tasks are cancelled and the pipeline returns a partial response from whatever `ctx.events` accumulated before cancellation. Default: no timeout. The API layer can set this via a request parameter or a per-depth default (e.g., 30s for cheap, 60s for standard, 120s for deep).
