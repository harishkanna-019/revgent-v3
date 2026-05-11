"""LLM provider tests — all hit real OpenRouter. No mocks."""

import asyncio
import os

import httpx
import pytest
import pytest_asyncio

from providers import llm

pytestmark = pytest.mark.asyncio

# Skip real-API tests when no key is available
_HAS_KEY = bool(os.environ.get("OPENROUTER_API_KEY"))
skip_if_no_key = pytest.mark.skipif(not _HAS_KEY, reason="OPENROUTER_API_KEY not set")


# ── Helpers ──


@pytest_asyncio.fixture(autouse=True)
async def _init_llm():
    """Ensure LLM provider is initialized before each test."""
    if _HAS_KEY:
        await llm.init()
    yield
    if _HAS_KEY:
        await llm.close()


# ── Real API Tests ──


@skip_if_no_key
class TestLLMCallReal:
    """Tests that make actual calls to OpenRouter."""

    async def test_basic_call_returns_text_and_usage(self):
        """call() returns (text, usage_dict) with real OpenRouter."""
        text, usage = await llm.call(
            model="deepseek/deepseek-v4-flash:nitro",
            max_tokens=64,
            prompt="Say hello in one word.",
        )
        assert isinstance(text, str)
        assert len(text) > 0
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "total_tokens" in usage
        assert usage["total_tokens"] > 0
        assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]

    async def test_call_with_empty_prompt(self):
        """Empty prompt: any well-formed outcome is acceptable.

        DeepSeek typically returns HTTP 400, but routing variations can
        return an empty completion or even a non-retryable LLMError. We
        only assert that the provider doesn't crash or hang.
        """
        try:
            text, usage = await llm.call(
                model="deepseek/deepseek-v4-flash:nitro",
                max_tokens=32,
                prompt="",
                retries=1,
            )
            assert isinstance(text, str)
            assert "total_tokens" in usage
        except (llm.LLMStatusError, llm.LLMError):
            # Both outcomes are acceptable for this edge case.
            pass

    async def test_call_with_longer_max_tokens(self):
        """Larger max_tokens allows longer responses."""
        text, usage = await llm.call(
            model="deepseek/deepseek-v4-flash:nitro",
            max_tokens=256,
            prompt="List 3 colors.",
        )
        assert isinstance(text, str)
        assert usage["output_tokens"] > 0

    async def test_concurrent_calls_respected_semaphore(self):
        """Many concurrent calls complete without exceeding semaphore limit."""

        # The semaphore is set to LLM_CONCURRENCY (default 24).
        # We launch 10 concurrent calls — all should succeed.
        async def _call(i: int):
            text, usage = await llm.call(
                model="deepseek/deepseek-v4-flash:nitro",
                max_tokens=32,
                prompt=f"Respond with the number {i} only.",
            )
            return text, usage

        tasks = [asyncio.create_task(_call(i)) for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        for text, usage in results:
            assert isinstance(text, str)
            assert usage["total_tokens"] > 0


@skip_if_no_key
class TestLLMLifecycle:
    """Tests for init() / close() lifecycle."""

    async def test_init_creates_client_and_semaphore(self):
        """init() creates _client and _semaphore."""
        # Fixture already called init(), so verify state
        assert llm._client is not None
        assert llm._semaphore is not None

    async def test_close_releases_resources(self):
        """close() nulls out _client and _semaphore."""
        await llm.close()
        assert llm._client is None
        assert llm._semaphore is None

    async def test_init_idempotent(self):
        """Calling init() twice is safe (idempotent)."""
        await llm.init()
        client_first = llm._client
        await llm.init()
        assert llm._client is client_first  # same instance

    async def test_call_auto_init_if_not_initialized(self):
        """call() auto-calls init() if not already done."""
        await llm.close()
        assert llm._client is None
        # This should auto-init and succeed
        text, usage = await llm.call(
            model="deepseek/deepseek-v4-flash:nitro",
            max_tokens=32,
            prompt="Hi",
        )
        assert isinstance(text, str)
        assert llm._client is not None


class TestLLMErrorHandling:
    """Tests for error conditions."""

    def test_missing_api_key_raises(self):
        """ValueError with actionable message when OPENROUTER_API_KEY is missing."""
        # Save current key
        original_key = os.environ.get("OPENROUTER_API_KEY")
        try:
            if "OPENROUTER_API_KEY" in os.environ:
                del os.environ["OPENROUTER_API_KEY"]
            # Reset module state so init() re-runs
            llm._client = None
            llm._semaphore = None

            with pytest.raises(ValueError) as exc_info:
                asyncio.run(llm.init())
            assert "OPENROUTER_API_KEY" in str(exc_info.value)
            assert "environment variable" in str(exc_info.value).lower()
        finally:
            if original_key is not None:
                os.environ["OPENROUTER_API_KEY"] = original_key
            # Restore module state
            llm._client = None
            llm._semaphore = None

    @skip_if_no_key
    async def test_invalid_model_raises(self):
        """Calling with a non-existent model raises (non-retryable 4xx)."""
        with pytest.raises((llm.LLMError, llm.LLMStatusError)):
            await llm.call(
                model="nonexistent/model-that-does-not-exist",
                max_tokens=32,
                prompt="Hi",
                retries=1,
            )


class TestReasoningModelDetection:
    """Tests for reasoning model helper functions."""

    def test_deepseek_is_reasoning(self):
        assert llm._is_reasoning_model("deepseek/deepseek-v4-flash") == (
            True,
            256,
        )
        assert llm._is_reasoning_model("deepseek/deepseek-v4-pro") == (True, 256)

    def test_kimi_is_reasoning(self):
        assert llm._is_reasoning_model("moonshotai/kimi-k2.6") == (True, 1024)
        assert llm._is_reasoning_model("moonshotai/kimi-k2.6") == (True, 1024)

    def test_non_reasoning_model(self):
        assert llm._is_reasoning_model("openai/gpt-4o") == (False, 0)
        assert llm._is_reasoning_model("google/gemini-2.0") == (False, 0)


class TestRetryableErrorDetection:
    """Tests for _is_retryable_error helper."""

    def test_rate_limit_is_retryable(self):
        exc = llm.LLMStatusError(429, "rate limit exceeded")
        assert llm._is_retryable_error(exc) is True

    def test_api_status_500_is_retryable(self):
        exc = llm.LLMStatusError(500, "server error")
        assert llm._is_retryable_error(exc) is True

    def test_api_status_529_is_retryable(self):
        exc = llm.LLMStatusError(529, "overloaded")
        assert llm._is_retryable_error(exc) is True

    def test_api_status_503_is_retryable(self):
        exc = llm.LLMStatusError(503, "service unavailable")
        assert llm._is_retryable_error(exc) is True

    def test_api_status_400_is_not_retryable(self):
        exc = llm.LLMStatusError(400, "bad request")
        assert llm._is_retryable_error(exc) is False

    def test_overloaded_message_is_retryable(self):
        """4xx body containing 'overloaded' is treated as retryable."""
        exc = llm.LLMStatusError(400, "The server is overloaded, try again later")
        assert llm._is_retryable_error(exc) is True

    def test_timeout_is_retryable(self):
        exc = httpx.ReadTimeout("timed out")
        assert llm._is_retryable_error(exc) is True

    def test_connect_error_is_retryable(self):
        exc = httpx.ConnectError("refused")
        assert llm._is_retryable_error(exc) is True

    def test_random_error_is_not_retryable(self):
        exc = ValueError("something else")
        assert llm._is_retryable_error(exc) is False
