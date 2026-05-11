"""Shared test fixtures.

pytest-asyncio creates a new event loop per test. Module-level provider state
(httpx.AsyncClient, asyncio.Semaphore) is bound to the loop it was created in,
so reusing it across tests causes "Event loop is closed" errors.

This fixture resets provider module state between tests so each test gets a
fresh client bound to its own loop. It does NOT touch caches that are
loop-independent (the company name TTL cache is pure data).
"""

from __future__ import annotations

import pytest

from providers import llm, scrape, search


@pytest.fixture(autouse=True)
def _reset_provider_state():
    """Reset provider clients/semaphores between tests to avoid loop reuse."""
    # Pre-test: ensure clean state
    llm._client = None
    llm._semaphore = None
    llm._api_key = None
    search._client = None
    search._semaphore = None
    scrape._client = None
    scrape._semaphore = None

    yield

    # Post-test: discard clients without aclose() (their loop has closed).
    # httpx tolerates this; the OS will reclaim sockets.
    llm._client = None
    llm._semaphore = None
    llm._api_key = None
    search._client = None
    search._semaphore = None
    scrape._client = None
    scrape._semaphore = None
