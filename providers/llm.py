"""LLM provider — AsyncAnthropic via OpenRouter with semaphore, retry, and reasoning model handling."""

import asyncio
import os

import anthropic

# Module-level state (initialized via init(), closed via close())
_client: anthropic.AsyncAnthropic | None = None
_semaphore: asyncio.Semaphore | None = None

# Retry backoff delays in seconds
_BACKOFF_DELAYS = [2, 4, 6]

# Reasoning model configuration
_REASONING_FLOORS = {
    "deepseek/": 256,
    "moonshotai/kimi-": 1024,
}


def _is_reasoning_model(model: str) -> tuple[bool, int]:
    """Detect if a model is a reasoning model and return its token floor."""
    for prefix, floor in _REASONING_FLOORS.items():
        if model.startswith(prefix):
            return True, floor
    return False, 0


def _is_retryable_error(exc: Exception) -> bool:
    """Check if an exception warrants a retry."""
    # Rate limit always retries
    if isinstance(exc, anthropic.RateLimitError):
        return True

    # API status errors: retry on 500, 529 (overloaded)
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", 0)
        if code in (500, 529):
            return True
        # Some providers overload as 503
        if code == 503:
            return True

    # Check error message for overloaded/rate-limit indicators
    msg = str(exc).lower()
    if any(
        word in msg
        for word in ("overloaded", "rate limit", "too many requests", "capacity")
    ):
        return True

    return False


async def init() -> None:
    """Initialize the LLM client and semaphore in the running event loop.

    Must be called before any LLM requests. Safe to call multiple times
    (idempotent — skips if already initialized).
    """
    global _client, _semaphore

    if _client is not None:
        return

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError(
            "OPENROUTER_API_KEY environment variable is required. "
            "Set it to your OpenRouter API key."
        )

    concurrency = int(os.environ.get("LLM_CONCURRENCY", "24"))

    _client = anthropic.AsyncAnthropic(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://revenanas.com",
            "X-Title": "Revgent v3",
        },
    )
    _semaphore = asyncio.Semaphore(concurrency)


async def close() -> None:
    """Close the LLM client and release resources."""
    global _client, _semaphore

    if _client is not None:
        await _client.close()
        _client = None
    _semaphore = None


async def call(
    model: str,
    max_tokens: int,
    prompt: str,
    retries: int = 3,
) -> tuple[str, dict]:
    """Call an LLM via OpenRouter with semaphore gating and retry.

    Args:
        model: OpenRouter model identifier (e.g. "deepseek/deepseek-v4-flash:nitro")
        max_tokens: Maximum tokens for the response
        prompt: User message content
        retries: Number of retry attempts on transient failures

    Returns:
        (response_text, usage_dict) where usage_dict has
        {input_tokens, output_tokens, total_tokens}

    Raises:
        ValueError: If OPENROUTER_API_KEY is missing (catches init() not called)
        RuntimeError: After exhausting all retries
    """
    if _client is None:
        # Auto-init if not already done (e.g., direct script usage without FastAPI lifespan)
        await init()

    assert _client is not None
    assert _semaphore is not None

    is_reasoning, token_floor = _is_reasoning_model(model)
    effective_max_tokens = max(max_tokens, token_floor) if is_reasoning else max_tokens

    last_error: Exception | None = None

    for attempt in range(retries):
        async with _semaphore:
            try:
                kwargs: dict = {
                    "model": model,
                    "max_tokens": effective_max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }

                # Reasoning model: minimal effort, exclude reasoning content from output
                if is_reasoning:
                    kwargs["extra_body"] = {
                        "reasoning": {
                            "effort": "minimal",
                            "exclude": True,
                        }
                    }

                response = await _client.messages.create(**kwargs)

                text = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        text += block.text

                # Empty-text retry for reasoning models: double max_tokens up to 4096
                if is_reasoning and not text.strip():
                    new_max = min(effective_max_tokens * 2, 4096)
                    if new_max > effective_max_tokens:
                        effective_max_tokens = new_max
                        continue  # retry immediately without counting as an attempt

                usage = {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "total_tokens": response.usage.input_tokens
                    + response.usage.output_tokens,
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
    raise RuntimeError(
        f"LLM call failed after {retries} attempts. "
        f"Model: {model}, Last error: {last_error}"
    ) from last_error
