"""Four-stage stop protocol filter — pure sync function."""

from datetime import datetime, timedelta
from urllib.parse import urlparse

# ── Excluded domains (13 social/media sites) ──

EXCLUDED_DOMAINS = frozenset({
    "facebook.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "reddit.com",
    "medium.com",
    "tiktok.com",
    "instagram.com",
    "youtube.com",
    "quora.com",
    "tumblr.com",
    "pinterest.com",
    "threads.net",
})


def _extract_domain(url: str) -> str:
    """Extract the netloc from a URL, stripping www. prefix."""
    if not url:
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _is_excluded_domain(url: str) -> bool:
    """Check if a URL's domain is in the excluded list."""
    domain = _extract_domain(url)
    if not domain:
        return False
    # Check exact match or suffix match (e.g., m.facebook.com)
    if domain in EXCLUDED_DOMAINS:
        return True
    # Check if any excluded domain is a suffix
    for excluded in EXCLUDED_DOMAINS:
        if domain.endswith(f".{excluded}"):
            return True
    return False


def _matches_keywords(text: str, keywords: list[str]) -> bool:
    """Check if any keyword appears in the text (case-insensitive)."""
    if not text or not keywords:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _parse_date_for_comparison(date_str: str) -> datetime | None:
    """Parse a YYYY-MM-DD date string for comparison."""
    if not date_str or date_str == "Unknown":
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


def apply_stop_protocol(
    results: list[dict],
    topic: str,
    company_names: list[str] | None,
    min_days: int,
    max_days: int,
    topic_keywords: list[str],
) -> list[dict]:
    """Apply the four-stage stop protocol to filter search results.

    Stages (applied in order):
    1. Date check — published_date within [today - max_days, today - min_days].
       Missing date passes (SearXNG results often lack dates).
    2. Source credibility — reject excluded domains.
    3. Topic relevance — at least one keyword from topic_keywords must appear
       in title or content. Empty keywords → all rejected.
    4. Company relevance — check company_names against title and content.
       Skipped when company_names is None.

    Args:
        results: List of search result dicts.
        topic: The research topic (used for keyword fallback).
        company_names: Pre-resolved company name variations, or None to skip.
        min_days: Minimum age in days (inclusive).
        max_days: Maximum age in days (inclusive).
        topic_keywords: Keywords for topic relevance filtering.

    Returns:
        Filtered list of results passing all stages.
    """
    today = datetime.now()
    min_date = today - timedelta(days=max_days)
    max_date = today - timedelta(days=min_days)

    filtered: list[dict] = []

    for r in results:
        # ── Stage 1: Date check ──
        published_date = r.get("published_date", "Unknown")
        parsed = _parse_date_for_comparison(published_date)
        if parsed is not None:
            # Date is known — check window
            if not (min_date <= parsed <= max_date):
                continue
        # If date is Unknown, pass through

        # ── Stage 2: Source credibility ──
        url = r.get("url", "")
        if _is_excluded_domain(url):
            continue

        # ── Stage 3: Topic relevance ──
        title = r.get("title", "")
        content = r.get("content", "")
        combined_text = f"{title} {content}"

        if not topic_keywords:
            # Empty keywords → all rejected
            continue

        if not _matches_keywords(combined_text, topic_keywords):
            continue

        # ── Stage 4: Company relevance ──
        if company_names is not None:
            if not _matches_keywords(combined_text, company_names):
                continue

        filtered.append(r)

    return filtered
