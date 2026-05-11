# Module Reference

Every module, its public interface, what it depends on, and what it owns.

---

## Provider Layer

### `providers/llm.py`

**Purpose:** All LLM calls to OpenRouter.

**Public interface:**
```python
async def call(model: str, max_tokens: int, prompt: str, retries: int = 3) -> tuple[str, dict]
```
- `model`: OpenRouter model identifier (e.g., `"deepseek/deepseek-v4-flash:nitro"`)
- `max_tokens`: budget for response text (reasoning models may get a higher effective cap)
- `prompt`: user message content
- Returns: `(response_text, {"input_tokens": int, "output_tokens": int, "total_tokens": int})`
- Raises: `RuntimeError` after exhausting retries. `ValueError` if `OPENROUTER_API_KEY` is not set.

**Owns:**
- One `anthropic.AsyncAnthropic` instance (lazy-initialized, shared across all requests)
- One `asyncio.Semaphore` gated by `LLM_CONCURRENCY` env var (default 24)
- Retry logic: 500, 529, rate limit, overloaded → async sleep 2s/4s/6s
- Reasoning model handling: token floor enforcement (deepseek→256, kimi→1024), empty-text retry with doubled cap up to 4096, `reasoning.effort=minimal` + `exclude=True`

**Depends on:** `anthropic` SDK, `asyncio`, `os`

---

### `providers/search.py`

**Purpose:** News search via self-hosted SearXNG.

**Public interface:**
```python
async def search(query: str, max_days: int = 90, limit: int = 10) -> list[dict]
async def search_many(queries: list[str], max_days: int = 90, limit: int = 10) -> list[dict]
```
- `search`: single query → list of result dicts `{"title", "url", "content", "published_date"}`
- `search_many`: multiple queries concurrently, results in query order, flattened. Per-query errors are isolated — a failed query contributes `[]` to its position, does not abort other queries.
- Raises: `SearchCircuitOpen` when circuit breaker is tripped (from `search()`, propagated through `search_many()` only if ALL queries fail due to open circuit). `ValueError` if `SEARXNG_URL` is not set and default localhost is unreachable.

**Owns:**
- One `httpx.AsyncClient` instance (lazy-initialized, shared)
- One `asyncio.Semaphore` gated by `SEARCH_CONCURRENCY` env var (default 12)
- Circuit breaker: 2 consecutive failures → open for 30 seconds
- In-memory cache: dict keyed by `(query, max_days, limit)`, 60-second TTL
- Date parsing from SearXNG metadata: ISO, "N days ago", "N hours ago", DD/MM/YYYY, YYYY-MM-DD
- Time range mapping: max_days → SearXNG `time_range` param

**Depends on:** `httpx`, `asyncio`, `os`, `time`, `re`, `datetime`

---

### `providers/scrape.py`

**Purpose:** Fetch web pages and extract article text.

**Public interface:**
```python
async def scrape(url: str, max_chars: int | None = None) -> str
async def scrape_many(urls: list[str], max_chars: int | None = None) -> dict[str, str]
```
- `scrape`: fetch one URL, extract text via trafilatura. Returns text or empty string when page loads but extraction produces nothing useful (quality gate, not an error). Raises `SSRFBlocked` if URL targets a non-public address. Raises `ScrapeError` on network timeout or HTTP 5xx.
- `scrape_many`: concurrent scrape with per-URL error isolation. Returns `{url: text}` dict. Failed URLs map to empty string (error logged, does not abort other URLs).

**Owns:**
- One `httpx.AsyncClient` instance (lazy-initialized, shared)
- One `asyncio.Semaphore` gated by `SCRAPE_CONCURRENCY` env var (default 8)
- SSRF protection: scheme whitelist (http/https), DNS resolution validation (every IP must be public-routable), manual redirect following (max 5 hops, each validated), `@lru_cache(2048)` on DNS results
- trafilatura extraction via `asyncio.run_in_executor()` (CPU-bound)
- Article quality gate: minimum 80 chars, reject error page markers

**Depends on:** `httpx`, `trafilatura`, `asyncio`, `ipaddress`, `socket`, `urllib.parse`

**Exceptions defined:**
```python
class SSRFBlocked(Exception):
    """Raised when a URL targets a non-public IP address."""
    def __init__(self, url: str, reason: str): ...

class ScrapeError(Exception):
    """Raised on network failure (timeout, HTTP 5xx, connection refused)."""
    def __init__(self, url: str, reason: str): ...
```

---

## Tool Layer

All tools are async functions. Each takes `RunContext` (or specific inputs) and returns `ToolResult`.

### `tools/topic.py`

**Purpose:** Simplify free-form topic input and generate search keywords.

**Public interface:**
```python
async def analyze(ctx: RunContext) -> ToolResult
```
- Reads: `ctx.topic.original`, `ctx.policy.model_for_task("topic_simplification")`, `ctx.policy.model_for_task("keyword_generation")`
- Output: `ToolResult(output={"simplified": str, "keywords": list[str]}, usage=dict)`
- Behavior: skips simplification if topic is ≤3 words. Keywords cached for 24 hours.

**Depends on:** `providers/llm`, `cache` (keyword cache)

---

### `tools/queries.py`

**Purpose:** Generate search queries for a company + topic.

**Public interface:**
```python
async def generate(ctx: RunContext) -> ToolResult
```
- Reads: `ctx.topic.simplified`, `ctx.company`, `ctx.policy.model_for_task("query_generation")`
- Output: `ToolResult(output=list[str], usage=dict)` — list of 7–10 query strings
- Parses JSON array from LLM response. Falls back to `["{company} {topic}"]` on parse failure.

**Depends on:** `providers/llm`

---

### `tools/validate.py`

**Purpose:** Validate company relevance and fact-check a single candidate.

**Public interface:**
```python
async def validate_one(ctx: RunContext, candidate: dict) -> ToolResult
```
- Reads: `ctx.company`, `ctx.topic.simplified`, `ctx.policy.model_for_task("validation")`, `ctx.policy.model_for_task("fact_check")`
- Output: `ToolResult(output={"result": dict|None, "status": str, "original": dict}, usage=dict, item_id=str)`
  - `status`: `"valid"` | `"opinion"` | `"not_about_company"`
- Behavior: validation first (with step-by-step retry on UNCERTAIN). If valid, fact-check. If invalid, returns immediately (no fact-check call — saves cost).

**Depends on:** `providers/llm`

---

### `tools/format.py`

**Purpose:** Generate AI summary and classify content type for a validated result.

**Public interface:**
```python
async def format_one(ctx: RunContext, candidate: dict) -> ToolResult
```
- Reads: `ctx.company`, `ctx.topic.simplified`, `ctx.policy.model_for_task("summarization")`, `ctx.policy.model_for_task("classification")`
- Output: `ToolResult(output=dict, usage=dict, item_id=str)` — output is a formatted event dict
- Behavior: summary and classification are independent LLM calls. Both fired concurrently via `asyncio.create_task()`. Content type validated against `{"novel_fact", "report", "analysis", "historical"}`, defaults to `"analysis"`.

**Depends on:** `providers/llm`, `formatting` (format_event, headline_has_numbers)

---

### `tools/company.py`

**Purpose:** Extract company name variations from a domain.

**Public interface:**
```python
async def get_names(company_domain: str) -> tuple[list[str], dict]
```
- Returns: `(["meta", "meta platforms", "facebook", "fb"], usage_dict)`
- Behavior: cached for 24 hours. Always includes the domain stem (e.g., "meta" from "meta.com"). Falls back to `[stem]` on LLM failure.

**Depends on:** `providers/llm`, `cache` (company name cache)

---

## Filter Layer

All filters are sync, pure functions. No I/O, no provider calls, no side effects.

### `filters/stop_protocol.py`

**Purpose:** Four-stage filter removing irrelevant search results.

**Public interface:**
```python
def apply_stop_protocol(
    results: list[dict],
    topic: str,
    company_names: list[str] | None,
    min_days: int,
    max_days: int,
    topic_keywords: list[str],
) -> list[dict]
```
- `company_names`: pre-resolved list of company name variations (e.g., `["meta", "meta platforms", "facebook"]`). The pipeline resolves these once per run via `tools/company.get_names()` before calling any filters. Pass `None` to skip the company relevance check (cheap mode uses regex instead).

**Filter stages (applied in order):**
1. **Date check** — published_date within `[today - max_days, today - min_days]`. Missing date passes (SearXNG results often lack dates).
2. **Source credibility** — rejects 13 excluded domains: facebook.com, twitter.com, x.com, linkedin.com, reddit.com, medium.com, tiktok.com, instagram.com, youtube.com, quora.com, tumblr.com, pinterest.com, threads.net
3. **Topic relevance** — at least one keyword from `topic_keywords` must appear in title or content. Empty keywords → all rejected.
4. **Company relevance** — checks pre-resolved `company_names` against title and content. Skipped when `company_names` is None.

**Depends on:** nothing (pure function — company names pre-resolved by pipeline)

---

### `filters/dedup.py`

**Purpose:** Remove duplicate URLs, preserving first occurrence order.

**Public interface:**
```python
def dedup_urls(results: list[dict]) -> list[dict]
```

---

### `filters/ranker.py`

**Purpose:** Rank candidates by metadata signals without any external calls.

**Public interface:**
```python
def rank(candidates: list[dict], topic_keywords: list[str]) -> list[dict]
```

**Scoring factors:**
- Recency: ≤1 day (+30), ≤7 days (+20), ≤30 days (+10), ≤90 days (+5)
- Keyword match in title: +15 per keyword
- Keyword match in content: +5 per keyword
- Source credibility: +10 for known credible domains (reuters, bloomberg, ft, wsj, nytimes, techcrunch, theguardian, bbc, cnbc, forbes, businessinsider, apnews, washingtonpost)
- Headline has numbers: +5
- Content length: >500 chars (+5), >200 (+3), >50 (+1)

Returns candidates sorted by score descending.

---

### `filters/signals.py`

**Purpose:** Route validated-but-not-hard-fact results to the signals lane.

**Public interface:**
```python
def classify_result(
    result: dict,
    is_valid: bool,
    is_hard_fact: bool,
    fact_check_raw: str | None,
    topic: str,
) -> LaneDecision
```

**Lane routing:**
- `is_valid and is_hard_fact` → `LaneDecision(lane="event")`
- `not is_valid` → `LaneDecision(lane="discard")`
- `is_valid and not is_hard_fact` → `LaneDecision(lane="signal")` with:
  - `signal_type`: market_speculation / unconfirmed / early_report / analyst_commentary
  - `confidence`: 0.35 / 0.40 / 0.55 / 0.65

---

## Core Layer

### `core/types.py`

**Purpose:** All shared type definitions. No runtime code.

**Types:**
```python
@dataclass(frozen=True)
class ToolResult:
    output: Any = None
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    item_id: str | None = None

@dataclass(frozen=True)
class StageStart:
    type: str = "stage_start"
    stage: str = ""
    count: int = 0

@dataclass(frozen=True)
class StageEnd:
    type: str = "stage_end"
    stage: str = ""
    out: int = 0

@dataclass(frozen=True)
class ItemResult:
    type: str = "item_result"
    stage: str = ""
    item_id: str = ""
    status: str = ""

@dataclass(frozen=True)
class BudgetCheck:
    type: str = "budget"
    spent: float = 0.0
    remaining: float = 0.0

Event = StageStart | StageEnd | ItemResult | BudgetCheck
Emit = Callable[[Event], None] | None
```

---

### `core/context.py`

**Purpose:** Per-request mutable state.

**Public interface:**
```python
class RunContext:
    # Immutable (set at creation)
    policy: ResearchDepthPolicy
    company: str
    topics: list[str]
    date_min: int
    date_max: int

    # Mutable accumulators
    cost: CostTracker
    usage: UsageStats
    events: list[dict]
    signals: list[dict]
    topic: TopicState | None

    @property
    def exhausted(self) -> bool: ...
    def record(self, usage: dict, item_id: str | None = None) -> None: ...
    def build_response(self, topic_name: str) -> dict: ...

@dataclass
class TopicState:
    original: str
    simplified: str = ""
    keywords: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
```

Uses `__slots__` for memory efficiency. Never shared across requests.

---

### `core/depth.py`

**Purpose:** Research depth configuration. Frozen, immutable per-request.

**Public interface:**
```python
@dataclass(frozen=True)
class ResearchDepthPolicy:
    depth: str
    max_candidates_per_topic: int
    max_queries_per_topic: int
    max_extraction_chars: int
    max_full_extraction_candidates: int
    default_budget: float
    max_workers: int

    def model_for_task(self, task: str) -> str: ...

    @classmethod
    def from_request(cls, depth: str = "cheap", max_cost: float | None = None) -> ResearchDepthPolicy: ...
```

**Task names for model routing:** `topic_simplification`, `keyword_generation`, `query_generation`, `validation`, `fact_check`, `summarization`, `classification`

Unchanged from v2.

---

### `core/runner.py`

**Purpose:** Parallel async execution with bounded concurrency and source-order results.

**Public interface:**
```python
async def parallel(
    fn: Callable[[Any], Awaitable[Any]],
    items: list[Any],
    max_workers: int,
) -> list[Any | BaseException]
```

Return type includes `BaseException` because `return_exceptions=True` means exceptions are returned as values, not raised. Callers MUST check `isinstance(result, BaseException)` for each item.

Uses `asyncio.gather(*tasks, return_exceptions=True)` + local `asyncio.Semaphore(max_workers)`. Returns results in input order. Exception results are returned as-is (not raised) — the caller inspects each result and handles exceptions per-item. This is the same error isolation pattern as pi-agent-core's `executeToolCallsParallel`, where one tool failure does not cancel other tools.

---

### `core/pipeline.py`

**Purpose:** The research pipeline. One function.

**Public interface:**
```python
async def run(ctx: RunContext, emit: Emit = None) -> dict
```

Returns the complete response dict (identical shape to v2 `ResearchResponse`).

---

## Shared Modules

### `models.py`

**Classes:**
- `UsageStats` — token accumulator with `add(usage_dict)` and `to_dict()`
- `CostTracker` — USD cost accumulator with budget enforcement, per-item attribution, breakdown by category/provider
- `ResearchSignal` — frozen dataclass for signal lane output

Unchanged from v2.

### `formatting.py`

**Functions:**
- `parse_date(date_str) → str` — parse ISO/RFC/various formats to YYYY-MM-DD, return "Unknown" on failure
- `format_event(result, topic, summary=None) → dict` — normalize search result to event dict shape
- `headline_has_numbers(headline) → bool` — regex check for specific numbers in headlines
- `extract_date_from_content(content) → str` — regex extraction of dates from article text (moved from date_extractor.py, only the pure extraction part)

### `answer_builder.py`

**Functions:**
- `build_answers(events, topics) → list[dict]` — build per-topic validated answer objects

Unchanged from v2.

### `cache.py`

**Class:**
```python
class AsyncTTLCache:
    def __init__(self, ttl_seconds: int = 86400): ...
    def get(self, key: str) -> Any | None: ...           # lock-free read
    async def set(self, key: str, value: Any) -> None: ...  # locked write
    async def get_or_compute(self, key: str, fn: Callable[[], Awaitable[Any]]) -> Any: ...
```

`get_or_compute` uses a per-key lock (not per-cache) to prevent thundering herd. If 100 concurrent requests all miss the cache for the same company domain, only one calls the LLM. The other 99 wait on the per-key lock and get the cached result. Implementation: internal `dict[str, asyncio.Lock]` keyed by cache key, cleaned up after compute completes.

**Instances (module-level in cache.py):**
- `keyword_cache` — 24h TTL, keyed by simplified topic
- `company_cache` — 24h TTL, keyed by company domain

Search cache lives inside `providers/search.py` (60s TTL, simpler dict — no thundering herd concern because search results are cheap and fast).

---

## Transport Layer

### `api.py`

**Endpoints:**
- `GET /` — health check → `{"status": "ok", "service": "Revgent API"}`
- `POST /research` — sync research → `ResearchResponse`
- `POST /research/async` — async research with webhook callback → `{"status": "processing", ...}`

All handlers are `async def`. No `threading.Thread`. Async endpoint uses `asyncio.create_task()`.

**Pydantic models:** Identical to v2 (ResearchRequest, AsyncResearchRequest, Event, Signal, Answer, ValidSource, ResearchResponse).
