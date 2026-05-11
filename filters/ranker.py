"""Metadata ranker — pure sync function."""

from datetime import datetime, timedelta
from urllib.parse import urlparse

# ── Credible domains bonus ──

CREDIBLE_DOMAINS = frozenset(
    {
        "reuters.com",
        "bloomberg.com",
        "ft.com",
        "wsj.com",
        "nytimes.com",
        "techcrunch.com",
        "theguardian.com",
        "bbc.com",
        "bbc.co.uk",
        "cnbc.com",
        "forbes.com",
        "businessinsider.com",
        "apnews.com",
        "washingtonpost.com",
    }
)


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


def _is_credible_domain(url: str) -> bool:
    """Check if a URL's domain is in the credible list."""
    domain = _extract_domain(url)
    if not domain:
        return False
    if domain in CREDIBLE_DOMAINS:
        return True
    for credible in CREDIBLE_DOMAINS:
        if domain.endswith(f".{credible}"):
            return True
    return False


def _score_recency(published_date: str) -> int:
    """Score based on how recent the article is."""
    if not published_date or published_date == "Unknown":
        return 0
    try:
        dt = datetime.strptime(published_date, "%Y-%m-%d")
        age = datetime.now() - dt
        if age <= timedelta(days=1):
            return 30
        if age <= timedelta(days=7):
            return 20
        if age <= timedelta(days=30):
            return 10
        if age <= timedelta(days=90):
            return 5
        return 0
    except ValueError:
        return 0


def _score_keyword_matches(text: str, keywords: list[str]) -> tuple[int, int]:
    """Score keyword matches in text. Returns (title_score, content_score)."""
    if not text or not keywords:
        return 0, 0
    text_lower = text.lower()
    matches = 0
    for kw in keywords:
        if kw.lower() in text_lower:
            matches += 1
    return matches, matches


def _headline_has_numbers(headline: str) -> bool:
    """Check if headline contains numeric tokens."""
    import re

    if not headline:
        return False
    return bool(re.search(r"\d", headline))


def _score_content_length(content: str) -> int:
    """Score based on content length."""
    length = len(content) if content else 0
    if length > 500:
        return 5
    if length > 200:
        return 3
    if length > 50:
        return 1
    return 0


def rank(candidates: list[dict], topic_keywords: list[str]) -> list[dict]:
    """Rank candidates by metadata signals.

    Scoring factors:
    - Recency: ≤1 day (+30), ≤7 days (+20), ≤30 days (+10), ≤90 days (+5)
    - Keyword match in title: +15 per keyword
    - Keyword match in content: +5 per keyword
    - Source credibility: +10 for known credible domains
    - Headline has numbers: +5
    - Content length: >500 chars (+5), >200 (+3), >50 (+1)

    Args:
        candidates: List of result dicts.
        topic_keywords: Keywords for scoring relevance.

    Returns:
        Candidates sorted by score descending.
    """
    scored: list[tuple[int, dict]] = []

    for c in candidates:
        score = 0

        # Recency
        score += _score_recency(c.get("published_date", ""))

        # Keyword matches in title
        title = c.get("title", "")
        title_matches, _ = _score_keyword_matches(title, topic_keywords)
        score += title_matches * 15

        # Keyword matches in content
        content = c.get("content", "")
        _, content_matches = _score_keyword_matches(content, topic_keywords)
        score += content_matches * 5

        # Source credibility
        url = c.get("url", "")
        if _is_credible_domain(url):
            score += 10

        # Headline has numbers
        if _headline_has_numbers(title):
            score += 5

        # Content length
        score += _score_content_length(content)

        scored.append((score, c))

    # Sort by score descending, stable for equal scores (preserves input order)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, c in scored]
