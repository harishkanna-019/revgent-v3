"""LLM provider - OpenRouter via OpenAI-compatible /chat/completions API.

OpenRouter's /v1/messages endpoint only supports Anthropic models. DeepSeek,
Kimi, and other non-Anthropic providers must use /v1/chat/completions
(OpenAI format). We use httpx directly to avoid coupling to either SDK.

Semaphore-gated with retry. Reasoning models (deepseek*, moonshotai/kimi*)
get a token floor and reasoning.effort=minimal/exclude=true to suppress
chain-of-thought from the visible response.
"""

import asyncio
import os

import httpx

# Module-level state (initialized via init(), closed via close())
_client: httpx.AsyncClient | None = None
_semaphore: asyncio.Semaphore | None = None
_api_key: str | None = None

# OpenRouter base URL (OpenAI-compatible endpoint)
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Retry backoff delays in seconds
_BACKOFF_DELAYS = [2, 4, 6]

# Reasoning model configuration
_REASONING_FLOORS = {
    "deepseek/": 256,
    "moonshotai/kimi-": 1024,
}

# HTTP timeout for a single LLM call
_REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)


class LLMError(RuntimeError):
    """Raised when an LLM call fails after exhausting retries."""


class LLMStatusError(Exception):
    """HTTP error from OpenRouter. Carries status_code for retry logic."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


def _is_reasoning_model(model: str) -> tuple[bool, int]:
    """Detect if a model is a reasoning model and return its token floor."""
    for prefix, floor in _REASONING_FLOORS.items():
        if model.startswith(prefix):
            return True, floor
    return False, 0


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an exception warrants a retry."""
    if isinstance(exc, LLMStatusError):
        # Retry on rate limit, server errors, overloaded
        if exc.status_code in (429, 500, 502, 503, 504, 529):
            return True
        msg = exc.body.lower()
        if any(
            word in msg
            for word in ("overloaded", "rate limit", "too many requests", "capacity")
        ):
            return True
        return False

    # Connection / timeout errors are retryable
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError)):
        return True

    return False


async def init() -> None:
    """Initialize the LLM client and semaphore in the running event loop.

    Reads OPENROUTER_API_KEY and LLM_CONCURRENCY from the environment at call
    time so tests and runtime configs can set them after import. Safe to call
    multiple times (idempotent).
    """
    global _client, _semaphore, _api_key

    if _client is not None:
        return

    _api_key = os.environ.get("OPENROUTER_API_KEY")
    if not _api_key:
        raise ValueError(
            "OPENROUTER_API_KEY environment variable is required. "
            "Set it to your OpenRouter API key."
        )

    concurrency = int(os.environ.get("LLM_CONCURRENCY", "24"))

    _client = httpx.AsyncClient(
        base_url=OPENROUTER_BASE_URL,
        timeout=_REQUEST_TIMEOUT,
        headers={
            "Authorization": f"Bearer {_api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://revenanas.com",
            "X-Title": "Revgent v3",
        },
    )
    _semaphore = asyncio.Semaphore(concurrency)


async def close() -> None:
    """Close the LLM client and release resources."""
    global _client, _semaphore, _api_key

    if _client is not None:
        await _client.aclose()
        _client = None
    _semaphore = None
    _api_key = None


async def call(
    model: str,
    max_tokens: int,
    prompt: str,
    retries: int = 3,
) -> tuple[str, dict]:
    """Call an LLM via OpenRouter with semaphore gating and retry.

    Args:
        model: OpenRouter model identifier (e.g. "deepseek/deepseek-v4-flash")
        max_tokens: Maximum tokens for the response
        prompt: User message content
        retries: Number of retry attempts on transient failures

    Returns:
        (response_text, usage_dict) where usage_dict has
        {input_tokens, output_tokens, total_tokens}

    Raises:
        ValueError: If OPENROUTER_API_KEY is missing (catches init() not called)
        LLMError: After exhausting all retries
        LLMStatusError: On non-retryable HTTP errors (4xx other than 429)
    """
    if _client is None:
        # Auto-init if not already done (e.g., direct script usage)
        await init()

    assert _client is not None
    assert _semaphore is not None

    is_reasoning, token_floor = _is_reasoning_model(model)
    effective_max_tokens = max(max_tokens, token_floor) if is_reasoning else max_tokens

    last_error: Exception | None = None

    for attempt in range(retries):
        async with _semaphore:
            try:
                payload: dict = {
                    "model": model,
                    "max_tokens": effective_max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }

                # Reasoning model: minimal effort, exclude reasoning from output
                if is_reasoning:
                    payload["reasoning"] = {
                        "effort": "minimal",
                        "exclude": True,
                    }

                response = await _client.post("/chat/completions", json=payload)

                if response.status_code >= 400:
                    body = response.text
                    raise LLMStatusError(response.status_code, body)

                data = response.json()

                # Extract text from choices[0].message.content
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError(
                        f"LLM returned no choices. Model: {model}, body: {data}"
                    )
                message = choices[0].get("message") or {}
                text = message.get("content") or ""

                # Empty-text retry for reasoning models: double max_tokens up to 4096
                if is_reasoning and not text.strip():
                    new_max = min(effective_max_tokens * 2, 4096)
                    if new_max > effective_max_tokens:
                        effective_max_tokens = new_max
                        continue  # retry immediately without counting as an attempt

                # Usage: OpenAI format uses prompt_tokens / completion_tokens
                usage_raw = data.get("usage") or {}
                input_tokens = int(usage_raw.get("prompt_tokens", 0))
                output_tokens = int(usage_raw.get("completion_tokens", 0))
                usage = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                }
                return text, usage

            except Exception as exc:
                last_error = exc
                if not _is_retryable_error(exc):
                    raise
                if attempt < retries - 1:
                    delay = _BACKOFF_DELAYS[min(attempt, len(_BACKOFF_DELAYS) - 1)]
                    await asyncio.sleep(delay)

    # All retries exhausted
    raise LLMError(
        f"LLM call failed after {retries} attempts. "
        f"Model: {model}, Last error: {last_error}"
    ) from last_error
