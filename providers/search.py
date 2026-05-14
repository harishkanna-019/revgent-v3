"""SearXNG search provider."""

import asyncio
import os
import time
from urllib.parse import urlencode

import httpx

from formatting import parse_date

# ── Configuration ──

# Constants (compile-time, no env)
SEARCH_TIMEOUT = 15.0  # seconds per query
SEARCH_CACHE_TTL = 60  # seconds
CIRCUIT_FAILURE_THRESHOLD = 5
CIRCUIT_COOLDOWN_SECONDS = 15

# Env-driven settings are resolved inside init() so tests and runtime can set
# them after import (e.g., via .env loading or fixtures).

# ── Module state ──

_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
SEARXNG_URL: str = "http://localhost:8888"
SEARCH_CONCURRENCY: int = 12
SEARCH_ENGINES: str = "bing news,google,brave"

# Circuit breaker state
_consecutive_failures = 0
_circuit_open_until = 0.0

# In-memory cache: dict keyed by "query|max_days|limit", TTL expiration
# Single-threaded event loop — no locks needed
_cache: dict[str, tuple[float, list[dict]]] = {}


class SearchCircuitOpen(RuntimeError):
    """Raised when the SearXNG circuit breaker is open."""

    pass


# ── Lifecycle ──


async def init() -> None:
    """Initialize the search client and semaphore.

    Reads SEARXNG_URL and SEARCH_CONCURRENCY from the environment at call
    time so tests can configure them after import.
    """
    global _client, _semaphore, SEARXNG_URL, SEARCH_CONCURRENCY, SEARCH_ENGINES
    SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")
    SEARCH_CONCURRENCY = int(os.environ.get("SEARCH_CONCURRENCY", "12"))
    SEARCH_ENGINES = os.environ.get("SEARCH_ENGINES", "bing news,google,brave")
    if _client is None:
        _client = httpx.AsyncClient(timeout=SEARCH_TIMEOUT)
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(SEARCH_CONCURRENCY)


async def close() -> None:
    """Close the search client and release resources."""
    global _client, _semaphore
    if _client is not None:
        await _client.aclose()
        _client = None
    _semaphore = None


# ── Internal helpers ──


def _check_circuit() -> None:
    """Raise SearchCircuitOpen if the circuit breaker is active."""
    if time.monotonic() < _circuit_open_until:
        remaining = int(_circuit_open_until - time.monotonic())
        raise SearchCircuitOpen(f"SearXNG circuit open, {remaining}s remaining")


def _record_success() -> None:
    """Reset circuit breaker on success."""
    global _consecutive_failures
    _consecutive_failures = 0


def _record_failure() -> None:
    """Increment failure counter and trip circuit if threshold reached."""
    global _consecutive_failures, _circuit_open_until
    _consecutive_failures += 1
    if _consecutive_failures >= CIRCUIT_FAILURE_THRESHOLD:
        _circuit_open_until = time.monotonic() + CIRCUIT_COOLDOWN_SECONDS


def _cache_key(query: str, max_days: int, limit: int) -> str:
    """Build a cache key from query parameters."""
    return f"{query}|{max_days}|{limit}"


def _get_cached(key: str) -> list[dict] | None:
    """Return cached results if not expired, otherwise None."""
    if key not in _cache:
        return None
    expires_at, results = _cache[key]
    if time.monotonic() > expires_at:
        del _cache[key]
        return None
    return results


def _set_cached(key: str, results: list[dict]) -> None:
    """Store results in cache with TTL expiration."""
    _cache[key] = (time.monotonic() + SEARCH_CACHE_TTL, results)


def _max_days_to_time_range(max_days: int) -> str | None:
    """Map max_days to SearXNG time_range parameter."""
    if max_days <= 1:
        return "day"
    if max_days <= 7:
        return "week"
    if max_days <= 31:
        return "month"
    if max_days <= 365:
        return "year"
    return None


def _parse_searxng_result(raw: dict) -> dict:
    """Convert a raw SearXNG result dict to our normalized format."""
    # SearXNG returns various field names depending on engine
    title = raw.get("title", "")
    url = raw.get("url", "")
    content = raw.get("content", "") or raw.get("body", "") or ""

    # Date parsing. SearXNG engines populate different fields:
    #   bing news       -> pubdate ('2026-01-30 00:00:00') AND metadata
    #                      ('1/30/2026 | AOL') - publishedDate is None
    #   qwant news      -> publishedDate ('2026-05-12T16:09:00') in ISO
    #   duckduckgo news -> publishedDate ('2025-08-25T00:08:00') in ISO
    # We try fields in order of reliability and short-circuit on first hit.
    # 'metadata' is checked last because it's both noisy ('6 days ago | CBS')
    # and locale-specific ('1/30/2026' could be Jan 30 or Mar 1 depending
    # on locale, but US-format is the bing news convention).
    published_date = "Unknown"
    for field in ("publishedDate", "published_date", "pubdate", "date", "metadata"):
        val = raw.get(field)
        if val and isinstance(val, str) and val.strip():
            parsed = parse_date(val.strip())
            if parsed != "Unknown":
                published_date = parsed
                break

    return {
        "title": title,
        "url": url,
        "content": content,
        "published_date": published_date,
    }


# ── Public interface ──


async def search(
    query: str,
    max_days: int = 90,
    limit: int = 10,
) -> list[dict]:
    """Search SearXNG for a single query.

    Returns a list of normalized result dicts with keys:
        title, url, content, published_date

    Raises:
        SearchCircuitOpen: if the circuit breaker is active.
        RuntimeError: on network failure after circuit breaker logic.
    """
    # Lazy init
    if _client is None or _semaphore is None:
        await init()

    # Check circuit breaker
    _check_circuit()

    # Check cache
    key = _cache_key(query, max_days, limit)
    cached = _get_cached(key)
    if cached is not None:
        return cached

    # Build URL.
    # We use engines=X,Y,Z instead of categories=news because:
    #   1. The engines param acts as an allowlist (not additive with categories)
    #   2. Bing News + Google + Brave give 2x more relevant results than all
    #      17 news engines combined (benchmarked)
    #   3. 82% less upstream traffic reduces rate-limit risk on Google/Bing
    #   4. Bing News supports time_range (Google/Brave silently ignore it)
    # Fall back to bing news only if SEARCH_ENGINES is set to empty.
    engines = SEARCH_ENGINES.strip() if SEARCH_ENGINES else "bing news"
    params: dict[str, str | int] = {
        "format": "json",
        "engines": engines,
        "q": query,
        "pageno": 1,
        "language": "en",
    }
    time_range = _max_days_to_time_range(max_days)
    if time_range:
        params["time_range"] = time_range

    url = f"{SEARXNG_URL}/search?{urlencode(params)}"

    # Make request with semaphore
    assert _client is not None
    assert _semaphore is not None

    try:
        async with _semaphore:
            response = await _client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        _record_failure()
        raise RuntimeError(f"SearXNG search failed for query '{query}': {exc}") from exc

    # Parse results
    raw_results = data.get("results", [])
    results = [_parse_searxng_result(r) for r in raw_results[:limit]]

    # Record success and cache
    _record_success()
    _set_cached(key, results)

    return results


async def search_many(
    queries: list[str],
    max_days: int = 90,
    limit: int = 10,
) -> list[dict]:
    """Search SearXNG for multiple queries concurrently.

    Results are flattened and returned in query order (all results for
    query 0, then all results for query 1, etc.). Per-query errors are
    isolated — a failed query contributes [] to its position and does
    not abort other queries.

    Raises:
        SearchCircuitOpen: only if the circuit is open AND this would
        affect all queries (i.e., circuit is open before any query runs).
        Individual query network errors are caught internally.
    """
    if _client is None or _semaphore is None:
        await init()

    # If circuit is open before we start, raise immediately
    _check_circuit()

    async def _search_one(q: str) -> list[dict]:
        try:
            return await search(q, max_days=max_days, limit=limit)
        except SearchCircuitOpen:
            raise  # propagate circuit open
        except Exception:
            # Individual query failure — return empty list, circuit breaker
            # already recorded the failure in search()
            return []

    tasks = [asyncio.create_task(_search_one(q)) for q in queries]
    results_per_query = await asyncio.gather(*tasks, return_exceptions=True)

    # Flatten in query order, filtering out exceptions (treat as empty)
    flattened: list[dict] = []
    for r in results_per_query:
        if isinstance(r, list):
            flattened.extend(r)
        # exceptions become empty — already handled in _search_one

    return flattened
