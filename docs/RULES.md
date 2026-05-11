# Rules & Constraints

Mandatory rules for all code in this repository. Violations are bugs.

---

## 1. No Silent Failures

Every function that can fail MUST either succeed or raise an exception with actionable context.

**Prohibited patterns:**
```python
# BAD: returns empty on failure — caller has no idea what happened
def scrape(url):
    try:
        ...
    except Exception:
        return ""

# BAD: returns empty list — caller thinks "no results" not "search is broken"
def search(query):
    if circuit_open:
        return []
```

**Required patterns:**
```python
# GOOD: raises with context
async def scrape(url):
    if not _url_safe(url):
        raise SSRFBlocked(url, f"Host {parsed.hostname} resolves to private IP")
    try:
        ...
    except httpx.TimeoutException:
        raise ScrapeError(url, "Timed out after 10s")

# GOOD: circuit breaker raises, not returns empty
async def search(query):
    if _circuit_open():
        raise SearchCircuitOpen(f"SearXNG circuit open, {cooldown}s remaining")
```

**Scrape has two failure modes:**
- Quality gate failure: page loads, trafilatura produces <80 chars or error-page text. Returns empty string. This is not an error — the content exists but isn't useful article text.
- Network failure: timeout, connection refused, HTTP 5xx. Raises `ScrapeError(url, reason)`. The pipeline wraps scrape calls with per-item error isolation, so one failed page does not abort scraping of other pages.

**Search has two failure modes:**
- Circuit open: raises `SearchCircuitOpen`. The pipeline catches this at the stage boundary.
- Individual query network error: caught internally by `search()`, returns `[]` for that query, circuit breaker records the failure. `search_many()` isolates per-query — one failed query does not abort the fan-out.

**Lazy initialization:** Importing a module without API keys must not raise. Calling a provider function without the required key MUST raise immediately:
```python
# GOOD: import works, call fails with clear message
_client = None
def _get_client():
    global _client
    if _client is None:
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ValueError("OPENROUTER_API_KEY environment variable is not set")
        _client = anthropic.AsyncAnthropic(api_key=key, ...)
    return _client
```

---

## 2. No User-Created Threads

Zero `ThreadPoolExecutor()`. Zero `threading.Thread`. Zero `threading.Lock`. Zero `multiprocessing`.

**The only thread usage:**
- `asyncio.run_in_executor(None, ...)` for CPU-bound `trafilatura.extract()` (measured ~4ms per page). This uses asyncio's default `ThreadPoolExecutor` (size: `min(32, os.cpu_count() + 4)`, e.g. 15 on 11-core). The scrape semaphore (default 8) limits how many extractions are in-flight, so the default executor always has spare capacity.

**All concurrency uses:**
- `asyncio.Semaphore` for backpressure
- `asyncio.gather` for parallel fan-out (with `return_exceptions=True` for error isolation)
- `asyncio.create_task` for concurrent independent work
- `asyncio.sleep` for non-blocking delays

**Rationale:** Threads don't compose under async. A `ThreadPoolExecutor` inside an async handler blocks the event loop indirectly, creates per-call thread overhead, and introduces shared-state races that asyncio.Semaphore eliminates.

---

## 3. No Mocks in Tests

No `unittest.mock`. No `unittest.mock.patch`. No `monkeypatch`. No `MagicMock`. No module-level patching. No fake providers.

Tests call real infrastructure:
- LLM tests hit real OpenRouter
- Search tests hit real SearXNG
- Scrape tests fetch real web pages

Pure logic tests (filters, formatting, ranking, models) don't need infrastructure at all — they're called with real data structures and assert on real return values.

**Rationale:** Mocks test your assumptions about the provider, not the provider. When OpenRouter changes a response format, a mock-based test still passes. A real-call test catches it.

**Consequence:** Integration tests are slower (seconds, not milliseconds). This is acceptable. Pure logic tests remain sub-second. Separate test markers allow running fast tests only during development.

---

## 4. Async All The Way Down

Every function that performs I/O MUST be `async def`. Every I/O call MUST be `await`-ed.

**Prohibited:**
```python
# BAD: sync requests in an async codebase
import requests
response = requests.get(url)

# BAD: sync anthropic client
client = anthropic.Anthropic(...)
response = client.messages.create(...)

# BAD: time.sleep blocks the event loop
time.sleep(5)
```

**Required:**
```python
# GOOD
response = await httpx_client.get(url)
response = await async_anthropic_client.messages.create(...)
await asyncio.sleep(2)
```

**Pure functions** (filters, formatting, ranking) remain sync. They do no I/O and execute in microseconds. Do not make them async for consistency — async overhead on CPU-only work is waste.

---

## 5. Semaphore-Gated Provider Access

Every external call goes through a semaphore. No direct provider calls bypassing the gate.

```
Provider         Semaphore        Env Var              Default
─────────────────────────────────────────────────────────────
OpenRouter       LLM_SEM          LLM_CONCURRENCY      24
SearXNG          SEARCH_SEM       SEARCH_CONCURRENCY   12
trafilatura      SCRAPE_SEM       SCRAPE_CONCURRENCY   8
```

The semaphore lives inside the provider module. Tool functions and the pipeline never touch semaphores directly — they call the provider's public async function, which internally acquires the semaphore.

**Rationale:** Under 100 concurrent Clay requests, without semaphores you get 1000+ simultaneous OpenRouter calls. With `Semaphore(24)`, you get 24 in-flight and 976 queued. No rate limit storms, no wasted retries, predictable throughput.

---

## 6. Source-Order Preservation

When items are processed in parallel, results MUST be returned in input order.

`asyncio.gather(*tasks)` guarantees this — it returns results in the order tasks were created, not completion order. The `parallel()` runner function uses this guarantee.

**Rationale:** Deterministic output. Given the same input and search results, the pipeline produces the same event ordering. This matters for Clay, which displays events in the order received.

---

## 7. Budget Is Not An Error

Budget exhaustion (`ctx.exhausted == True`) produces a partial response, not an exception.

```python
# GOOD: break from topic loop, assemble what we have
for topic in ctx.topics:
    ...
    if ctx.exhausted:
        break
return ctx.build_response(...)

# BAD: raise on budget
if ctx.exhausted:
    raise BudgetExhausted()
```

A stage that starts will complete — budget checks happen BETWEEN stages, not inside them. This means actual spend may slightly exceed the budget (by the cost of the last stage's LLM calls), but no stage is interrupted mid-execution.

**Rationale:** Partial results are more useful than an error. A Clay enrichment that finds 2 events before exhausting its $0.01 budget is better than one that returns nothing.

---

## 8. One Pipeline Function

The entire research flow lives in one function: `core/pipeline.run()`. It reads top-to-bottom as a stage list.

**Prohibited:**
- Separate functions for cheap/standard/deep paths
- Stage logic split across multiple orchestrator files
- Stages that call other stages
- Recursive pipeline invocation

**Required:**
- Conditional branches within the one function for depth differences
- Each stage is a call to a tool function or a filter function
- Budget checks between stages (not inside tools)
- Event emission at stage boundaries

---

## 9. Provider ↔ Tool ↔ Pipeline Separation

Three distinct roles. Do not mix them.

**Providers** (`providers/*`):
- Own the external connection (httpx client, anthropic client)
- Own retry logic
- Own the semaphore
- Know nothing about topics, candidates, or the pipeline
- Interface: `async def call(model, max_tokens, prompt)`, `async def search(query)`, `async def scrape(url)`

**Tools** (`tools/*`):
- Compose providers with domain-specific prompts
- Parse LLM responses into structured output
- Know about topics, candidates, company domains
- Do NOT own retry logic (provider handles it)
- Do NOT own parallelism (pipeline calls `parallel()`)
- Interface: `async def analyze(ctx)`, `async def validate_one(ctx, candidate)`

**Pipeline** (`core/pipeline.py`):
- Orchestrates tool calls in sequence
- Calls `parallel()` for fan-out stages
- Enforces budget between stages
- Emits events
- Assembles the response
- Does NOT call providers directly (always through tools)

**Filters** (`filters/*`):
- Pure functions, sync
- Called by the pipeline between tool stages
- No provider calls, no tool calls
- No awareness of the pipeline's state (they receive inputs and return outputs)

---

## 10. Typed Events, Not Print Statements

All pipeline observability goes through the `emit` callback with typed event dataclasses.

**Prohibited:**
```python
print(f"\n[4] Validating with sub-agent...")
print(f"    VALID (hard fact): {title}...")
print(f"    -> {len(validated)} passed validation")
```

**Required:**
```python
emit(StageStart(stage="validate", count=len(candidates)))
emit(ItemResult(stage="validate", item_id=url, status="valid"))
emit(StageEnd(stage="validate", out=len(validated)))
```

The `emit` callback is optional (`None` means discard events). The pipeline never assumes emit does anything — it's purely observational.

**Rationale:** Typed events can be consumed by the API (progress webhooks), CLI (progress display), tests (assert on event sequence), and logging (structured JSON). Print statements can only be read by humans staring at a terminal.

---

## 11. Response Shape Is Frozen

The response dict returned by `pipeline.run()` MUST match the v2 `ResearchResponse` Pydantic model field-for-field. No fields added, removed, or renamed without a version bump.

**Frozen fields:**
```
company: str
events: list[Event]
answers: list[Answer]
signals: list[Signal]
usage: {"input_tokens", "output_tokens", "total_tokens"}
topic_results: {"topic_found", "topic_count", "topic_name"}
cost: {"total_cost", "budget", "budget_exhausted", "breakdown"}
budget: {"requested", "remaining", "exhausted"}
```

**Event fields:** headline, description, topic, date, source_name, source_url, content_type, headline_has_numbers, cost_attribution

**Signal fields:** headline, description, topic, date, source_name, source_url, signal_type, confidence, why_not_event, cost_attribution

---

## 12. SSRF Protection Is Mandatory

Every outbound HTTP request to a user-influenced URL MUST pass SSRF validation.

**Checks (all required, in order):**
1. URL scheme is `http` or `https` (no `file://`, `ftp://`, `gopher://`)
2. Hostname is present and non-empty
3. Hostname DNS resolution produces only public-routable IP addresses
4. Every IP in the DNS response is checked (not just the first)
5. The following are blocked: private (RFC 1918), loopback (127.0.0.0/8), link-local (169.254.0.0/16), multicast, reserved, unspecified
6. `localhost` and `*.localhost` are blocked by name (before DNS resolution)
7. Redirects are followed manually (max 5 hops). Each hop's URL is validated before following.
8. DNS resolution results are cached (`@lru_cache(2048)`) to avoid repeated lookups

**Applies to:** `providers/scrape.py` only. SearXNG calls go to a configured self-hosted URL (not user-influenced). LLM calls go to OpenRouter's fixed URL.

---

## 13. Dependencies

Only what's used. No vendoring libraries for features that don't exist.

```
anthropic>=0.40.0          # AsyncAnthropic for OpenRouter
httpx>=0.27.0              # Async HTTP (also anthropic transitive dep)
python-dotenv>=1.0.0       # .env loading
fastapi>=0.115.0           # API framework
uvicorn[standard]>=0.32.0  # ASGI server
trafilatura>=2.0.0         # HTML content extraction
pytest>=8.0.0              # Testing
pytest-asyncio>=0.24.0     # Async test support
```

**Explicitly excluded:** `tavily-python`, `firecrawl`, `requests`, `aiohttp`. If you find yourself reaching for one of these, you're solving the wrong problem.

---

## 14. Cache Semantics

Two categories of cache with different safety requirements:

**Cross-request caches** (keyword cache, company name cache):
- Long TTL (24 hours)
- Live in `cache.py` as `AsyncTTLCache` instances
- Lock-free reads, `asyncio.Lock`-protected writes
- Cache miss → compute → cache set (via `get_or_compute`)
- Tolerate a narrow race window: two concurrent misses may both compute and set. The second write overwrites the first with the same value. This is safe because LLM responses for the same input are functionally equivalent.

**Per-provider caches** (SearXNG search cache):
- Short TTL (60 seconds)
- Live inside the provider module
- Plain dict (single-threaded event loop, no races)
- Purpose: deduplicate identical queries within a single pipeline run or across near-simultaneous requests

**Thundering herd prevention:** `get_or_compute()` holds a per-key `asyncio.Lock`. If 100 requests miss the cache for the same key simultaneously, only one computes. The other 99 await the lock and get the cached result. This prevents redundant LLM calls when Clay sends 100 rows for the same company.

**No cache invalidation.** TTL expiration is the only eviction strategy. There is no manual invalidation, no cache clear API, no cache warming.

---

## 15. Environment Variables

All runtime configuration through environment variables. No config files, no YAML, no TOML.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENROUTER_API_KEY` | Yes | — | OpenRouter API authentication |
| `SEARXNG_URL` | No | `http://localhost:8888` | Self-hosted SearXNG instance URL |
| `LLM_CONCURRENCY` | No | `24` | Max concurrent OpenRouter calls |
| `SEARCH_CONCURRENCY` | No | `12` | Max concurrent SearXNG queries |
| `SCRAPE_CONCURRENCY` | No | `8` | Max concurrent page scrapes |
| `DEFAULT_MAX_COST` | No | (per-depth default) | Override default budget ceiling |
| `ABSOLUTE_MAX_COST` | No | — | Hard ceiling on any request's max_cost |
| `PORT` | No | `8000` | Uvicorn listen port (Railway sets this) |

Missing required variables raise `ValueError` on first provider call, not on import.

---

## 16. Provider Initialization via Lifespan

Provider clients (`AsyncAnthropic`, `httpx.AsyncClient`) and semaphores (`asyncio.Semaphore`) MUST be initialized inside a running event loop, not at module import time and not via lazy-init races.

Each provider exposes `async def init()` and `async def close()`. These are called from FastAPI's `lifespan` context manager:
- `init()`: creates the client instance and semaphore. Validates required env vars. Raises immediately if misconfigured.
- `close()`: closes httpx clients, releases connection pools.

This eliminates the lazy-init race where two coroutines simultaneously create two different semaphores or clients.

For CLI usage, `init()` is called explicitly before `pipeline.run()`.

---

## 17. Error Isolation in Parallel Stages

The `parallel()` runner uses `asyncio.gather(return_exceptions=True)`. This means:
- If 1 of 10 validation tasks raises a `RuntimeError` (LLM exhausted retries), the other 9 still complete.
- The pipeline receives a list of 10 results, where 9 are `ToolResult` and 1 is a `RuntimeError`.
- The pipeline MUST check `isinstance(result, BaseException)` for each item.
- Exception items are discarded and logged via `emit(ItemResult(..., status="error"))`.

This is the same pattern as pi-agent-core's `executePreparedToolCall`, where tool errors are caught per-tool and converted to error tool results instead of crashing the entire turn.

---

## 18. Graceful Shutdown

On SIGTERM:
1. Uvicorn stops accepting new connections (`--timeout-graceful-shutdown`, default 30s)
2. In-flight requests complete or are cancelled after the grace period
3. FastAPI lifespan exit block runs: `providers.search.close()`, `providers.scrape.close()`
4. Background webhook tasks (from `/research/async`) are tracked in a `set()` and awaited during shutdown

`asyncio.create_task()` results MUST be stored in a set to prevent garbage collection from cancelling them. The lifespan exit block awaits remaining tasks with a timeout.

---

## 19. Pipeline Timeout

`pipeline.run()` accepts an optional `timeout_seconds` parameter. When set, the entire pipeline is wrapped in `asyncio.wait_for()`. On timeout:
- In-flight LLM/search/scrape calls are cancelled
- The pipeline returns a partial response from whatever `ctx.events` accumulated
- Budget is not exceeded because cancelled calls don't complete

Default: no timeout. The API layer can set per-depth defaults (e.g., 30s cheap, 60s standard, 120s deep) or accept a request parameter.
