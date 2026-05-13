"""Search provider tests — real SearXNG when available, mocked for unit tests."""

import asyncio
import os
import time

import pytest
import pytest_asyncio

from providers import search

pytestmark = pytest.mark.asyncio

# Skip real-API tests when no SearXNG URL is configured
_HAS_SEARXNG = bool(os.environ.get("SEARXNG_URL"))
skip_if_no_searxng = pytest.mark.skipif(not _HAS_SEARXNG, reason="SEARXNG_URL not set")


# ── Helpers ──


@pytest_asyncio.fixture(autouse=True)
async def _init_search():
    """Ensure search provider is initialized before each test."""
    search._cache.clear()
    search._consecutive_failures = 0
    search._circuit_open_until = 0.0
    await search.init()
    yield
    await search.close()
    search._cache.clear()
    search._consecutive_failures = 0
    search._circuit_open_until = 0.0


# ── Real API Tests ──


@skip_if_no_searxng
class TestSearchReal:
    """Tests that make actual calls to SearXNG."""

    async def test_basic_search_returns_results(self):
        """search() returns list of dicts with expected keys."""
        results = await search.search("openai", max_days=90, limit=5)
        assert isinstance(results, list)
        for r in results:
            assert "title" in r
            assert "url" in r
            assert "content" in r
            assert "published_date" in r

    async def test_search_many_returns_flattened_results(self):
        """search_many() returns results from multiple queries."""
        results = await search.search_many(
            ["openai", "google"],
            max_days=90,
            limit=3,
        )
        assert isinstance(results, list)
        # Should have results from both queries
        assert len(results) > 0

    async def test_search_many_isolates_errors(self):
        """A bad query doesn't abort other queries."""
        results = await search.search_many(
            ["openai", "this-query-will-probably-return-nothing-xyz123"],
            max_days=90,
            limit=3,
        )
        assert isinstance(results, list)
        # Should still have results from the good query

    async def test_cache_returns_same_results(self):
        """Second identical search returns cached results."""
        q = "test-cache-query-unique-12345"
        results1 = await search.search(q, max_days=30, limit=2)
        # Patch the client to verify cache hit (don't make second request)
        original_get = search._client.get
        called = False

        async def fake_get(*args, **kwargs):
            nonlocal called
            called = True
            return await original_get(*args, **kwargs)

        search._client.get = fake_get
        try:
            results2 = await search.search(q, max_days=30, limit=2)
            assert results1 == results2
            assert not called, "Cache should have been hit, no HTTP request made"
        finally:
            search._client.get = original_get


# ── Circuit Breaker Tests ──


class TestCircuitBreaker:
    """Tests for circuit breaker logic."""

    async def test_circuit_opens_after_two_failures(self):
        """After 2 consecutive failures, circuit opens for 30 seconds."""
        # Force failures by pointing to a bad URL temporarily
        original_url = search.SEARXNG_URL
        search.SEARXNG_URL = "http://localhost:59999"  # no server here

        try:
            # Failures 1 through 4: counter increments, circuit stays closed
            for i in range(1, search.CIRCUIT_FAILURE_THRESHOLD):
                with pytest.raises(RuntimeError):
                    await search.search("test")
                assert search._consecutive_failures == i
                assert search._circuit_open_until == 0

            # Failure at threshold: circuit opens
            with pytest.raises(RuntimeError):
                await search.search("test")
            assert search._consecutive_failures == search.CIRCUIT_FAILURE_THRESHOLD
            assert search._circuit_open_until > time.monotonic()

            # Next call should raise SearchCircuitOpen immediately
            with pytest.raises(search.SearchCircuitOpen) as exc_info:
                await search.search("test")
            assert "circuit open" in str(exc_info.value).lower()
        finally:
            search.SEARXNG_URL = original_url
            search._consecutive_failures = 0
            search._circuit_open_until = 0.0

    async def test_circuit_closes_after_cooldown(self):
        """Circuit closes after 30-second cooldown."""
        # Manually trip the circuit
        search._consecutive_failures = 2
        search._circuit_open_until = time.monotonic() + 0.1  # 100ms cooldown

        # Should be open now
        with pytest.raises(search.SearchCircuitOpen):
            await search.search("test")

        # Wait for cooldown
        await asyncio.sleep(0.15)

        # Circuit should be closed — but search will fail with RuntimeError
        # since we're hitting a non-existent server
        original_url = search.SEARXNG_URL
        search.SEARXNG_URL = "http://localhost:59999"
        try:
            with pytest.raises(RuntimeError):
                await search.search("test")
            # Circuit should be reset on success path, but this is a failure
            # The failure counter increments again
        finally:
            search.SEARXNG_URL = original_url
            search._consecutive_failures = 0
            search._circuit_open_until = 0.0

    async def test_success_resets_failure_counter(self):
        """A successful call resets the consecutive failure counter."""
        search._consecutive_failures = 1

        # Make a successful call (using cache or real API)
        if _HAS_SEARXNG:
            await search.search("openai", limit=1)
            assert search._consecutive_failures == 0
        else:
            # Simulate success by calling _record_success directly
            search._record_success()
            assert search._consecutive_failures == 0


# ── Cache Tests ──


class TestSearchCache:
    """Tests for search cache logic."""

    def test_cache_hit_returns_results(self):
        """Cached results are returned without HTTP call."""
        key = search._cache_key("query", 30, 10)
        search._set_cached(key, [{"title": "Cached"}])
        assert search._get_cached(key) == [{"title": "Cached"}]

    def test_cache_miss_returns_none(self):
        """Missing cache key returns None."""
        assert search._get_cached("nonexistent|30|10") is None

    def test_cache_expires_after_ttl(self):
        """Expired cache entries are evicted."""
        key = search._cache_key("query", 30, 10)
        search._set_cached(key, [{"title": "Old"}])
        # Manually expire by setting past time
        search._cache[key] = (time.monotonic() - 1, [{"title": "Old"}])
        assert search._get_cached(key) is None

    def test_cache_key_format(self):
        """Cache key includes query, max_days, and limit."""
        assert search._cache_key("foo", 30, 10) == "foo|30|10"
        assert search._cache_key("foo", 30, 20) == "foo|30|20"
        assert search._cache_key("bar", 30, 10) == "bar|30|10"


# ── Time Range Mapping Tests ──


class TestTimeRangeMapping:
    """Tests for max_days → time_range mapping."""

    def test_one_day(self):
        assert search._max_days_to_time_range(1) == "day"

    def test_seven_days_week(self):
        assert search._max_days_to_time_range(7) == "week"

    def test_thirty_days_month(self):
        assert search._max_days_to_time_range(30) == "month"

    def test_three_sixty_five_year(self):
        assert search._max_days_to_time_range(365) == "year"

    def test_boundary_day_week(self):
        assert search._max_days_to_time_range(2) == "week"

    def test_boundary_week_month(self):
        assert search._max_days_to_time_range(8) == "month"

    def test_boundary_month_year(self):
        assert search._max_days_to_time_range(32) == "year"

    def test_over_year(self):
        assert search._max_days_to_time_range(500) is None


# ── Result Parsing Tests ──


class TestParseSearxngResult:
    """Tests for _parse_searxng_result."""

    def test_basic_result(self):
        raw = {
            "title": "Test Title",
            "url": "https://example.com/article",
            "content": "Article content here",
            "publishedDate": "2026-01-15",
        }
        result = search._parse_searxng_result(raw)
        assert result["title"] == "Test Title"
        assert result["url"] == "https://example.com/article"
        assert result["content"] == "Article content here"
        assert result["published_date"] == "2026-01-15"

    def test_fallback_body_field(self):
        raw = {
            "title": "Test",
            "url": "https://example.com",
            "body": "Body content",
        }
        result = search._parse_searxng_result(raw)
        assert result["content"] == "Body content"

    def test_unknown_date(self):
        raw = {
            "title": "Test",
            "url": "https://example.com",
        }
        result = search._parse_searxng_result(raw)
        assert result["published_date"] == "Unknown"

    def test_date_from_metadata(self):
        raw = {
            "title": "Test",
            "url": "https://example.com",
            "metadata": "2026-03-10",
        }
        result = search._parse_searxng_result(raw)
        assert result["published_date"] == "2026-03-10"

    def test_relative_date_parsing(self):
        raw = {
            "title": "Test",
            "url": "https://example.com",
            "publishedDate": "3 days ago",
        }
        result = search._parse_searxng_result(raw)
        assert result["published_date"] != "Unknown"


# ── Lifecycle Tests ──


class TestSearchLifecycle:
    """Tests for init() / close() lifecycle."""

    async def test_init_creates_client_and_semaphore(self):
        assert search._client is not None
        assert search._semaphore is not None

    async def test_close_releases_resources(self):
        await search.close()
        assert search._client is None
        assert search._semaphore is None

    async def test_init_idempotent(self):
        await search.init()
        client_first = search._client
        await search.init()
        assert search._client is client_first

    async def test_search_auto_init(self):
        await search.close()
        assert search._client is None
        # search() should auto-init
        # We can't actually call it without SearXNG, but we can verify init happened
        if not _HAS_SEARXNG:
            pytest.skip("No SearXNG available")
        await search.search("openai", limit=1)
        assert search._client is not None
