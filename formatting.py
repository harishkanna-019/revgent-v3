"""Pure formatting utilities. No I/O, no side effects."""

import re
from datetime import datetime, timedelta


def parse_date(date_str: str) -> str:
    """Parse a date string into YYYY-MM-DD format.

    Handles: ISO 8601, "N days ago", "N hours ago", DD/MM/YYYY, YYYY-MM-DD.
    Returns "Unknown" on failure.
    """
    if not date_str or date_str.strip().lower() == "unknown":
        return "Unknown"

    date_str = date_str.strip()

    # ISO 8601 (e.g., 2026-01-16 or 2026-01-16T10:30:00Z)
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # "N days ago"
    days_match = re.match(r"(\d+)\s+days?\s+ago", date_str, re.IGNORECASE)
    if days_match:
        days = int(days_match.group(1))
        dt = datetime.now() - timedelta(days=days)
        return dt.strftime("%Y-%m-%d")

    # "N hours ago"
    hours_match = re.match(r"(\d+)\s+hours?\s+ago", date_str, re.IGNORECASE)
    if hours_match:
        dt = datetime.now()
        return dt.strftime("%Y-%m-%d")

    # Strip trailing source attribution from bing news metadata format,
    # e.g. '1/30/2026 | AOL' -> '1/30/2026' or 'Jan 30, 2026 | Reuters' -> 'Jan 30, 2026'
    if "|" in date_str:
        date_str = date_str.split("|", 1)[0].strip()

    if not date_str:
        return "Unknown"

    # MM/DD/YYYY (US format, bing news convention)
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # DD/MM/YYYY
    try:
        dt = datetime.strptime(date_str, "%d/%m/%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass

    # YYYY-MM-DD (already in target format)
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        pass

    # "Mon DD, YYYY" / "Month DD, YYYY" (Bing News US wire format)
    # Try abbreviated month first since it's more common in feed metadata.
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # "DD Mon YYYY" / "DD Month YYYY" (UK/EU wire format - Reuters, Guardian)
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    return "Unknown"


def format_event(
    result: dict, summary: str | None = None, content_type: str = "analysis"
) -> dict:
    """Format a search result into a standard event dict.

    Args:
        result: Search result dict with title, url, content, published_date
        summary: AI-generated summary (optional, defaults to content snippet)
        content_type: One of novel_fact, report, analysis, historical

    Returns:
        Event dict matching the v2 ResearchResponse.events shape.
    """
    title = result.get("title", "")
    url = result.get("url", "")
    content = result.get("content", "")
    published_date = result.get("published_date", "")

    # Extract domain as source name
    source_name = ""
    if url:
        try:
            from urllib.parse import urlparse

            source_name = urlparse(url).netloc.replace("www.", "")
        except Exception:
            source_name = url

    # Resolve date: try published_date first, fall back to content extraction.
    date = parse_date(published_date) if published_date else "Unknown"
    if date == "Unknown" and content:
        date = extract_date_from_content(content)

    return {
        "headline": title,
        "description": summary if summary is not None else content[:400],
        "topic": "",
        "date": date,
        "source_name": source_name,
        "source_url": url,
        "content_type": content_type,
        "headline_has_numbers": headline_has_numbers(title),
        "cost_attribution": 0.0,
    }


def headline_has_numbers(headline: str) -> bool:
    """Check if a headline contains numeric tokens."""
    if not headline:
        return False
    return bool(re.search(r"\d", headline))


def extract_date_from_content(content: str) -> str:
    """Attempt to extract a date from article content using regex.

    Returns YYYY-MM-DD or "Unknown".
    """
    if not content:
        return "Unknown"

    # Look for YYYY-MM-DD patterns
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", content)
    if match:
        year, month, day = match.groups()
        try:
            dt = datetime(int(year), int(month), int(day))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Look for "Month DD, YYYY" or "DD Month YYYY"
    month_pattern = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    match = re.search(
        rf"({month_pattern})\s+(\d{{1,2}}),?\s+(\d{{4}})", content, re.IGNORECASE
    )
    if match:
        try:
            dt = datetime.strptime(match.group(0).replace(",", ""), "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    return "Unknown"
