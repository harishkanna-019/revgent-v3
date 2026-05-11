"""Deduplication filter — pure sync function."""


def dedup_urls(results: list[dict]) -> list[dict]:
    """Remove duplicate URLs, preserving first occurrence order.

    Args:
        results: List of result dicts, each with a "url" key.

    Returns:
        Filtered list with only the first occurrence of each URL.
    """
    seen: set[str] = set()
    deduped: list[dict] = []

    for r in results:
        url = r.get("url", "")
        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)
        deduped.append(r)

    return deduped
