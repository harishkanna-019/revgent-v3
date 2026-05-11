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

    async def _wrap(item: Any) -> T:
        async with semaphore:
            return await fn(item)

    tasks = [asyncio.create_task(_wrap(item)) for item in items]
    # return_exceptions=True converts raised exceptions into return values,
    # so one failed task does not cancel its siblings. Callers must check
    # isinstance(result, BaseException) for each element.
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return list(results)
