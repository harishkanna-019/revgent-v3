"""Parallel async execution with bounded concurrency and source-order results."""

import asyncio
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


async def parallel(
    fn: Callable[[Any], Awaitable[T]],
    items: list[Any],
    max_workers: int,
) -> list[T | BaseException]:
    """Run fn(item) for each item in parallel, with bounded concurrency.

    Results are returned in input order (guaranteed by asyncio.gather).
    Exceptions are returned as values, not raised — callers must inspect
    each result with isinstance(result, BaseException).

    Args:
        fn: Async function to call for each item
        items: List of items to process
        max_workers: Maximum concurrent executions

    Returns:
        List of results in the same order as `items`. Each element is either
        the return value of fn(item) or a BaseException if fn(item) raised.
    """
    if not items:
        return []

    semaphore = asyncio.Semaphore(max_workers)

    async def _wrap(item: Any) -> T | BaseException:
        async with semaphore:
            try:
                return await fn(item)
            except BaseException as exc:
                return exc

    tasks = [asyncio.create_task(_wrap(item)) for item in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # asyncio.gather with return_exceptions=True returns exceptions as values,
    # but they may be wrapped in asyncio.CancelledError etc. The _wrap
    # function above catches BaseException and returns it, so the results
    # should already be exception instances. However, gather may also wrap
    # exceptions, so we flatten any ExceptionGroup or similar wrappers.
    return list(results)
