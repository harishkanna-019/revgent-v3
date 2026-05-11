"""Build validated answer objects from events. Pure transforms."""

from urllib.parse import urlparse


def _sort_key(event: dict) -> tuple[int, str]:
    """Sort key for ordering events within a topic.

    Hard-fact events (content_type="novel_fact" or "report") rank first,
    then by date descending (most recent first). "Unknown" dates rank last.
    """
    content_type = (event.get("content_type") or "").lower()
    hard_fact = content_type in ("novel_fact", "report")
    # Lower tuple element sorts first; negate hard_fact so True (1) -> 0
    fact_rank = 0 if hard_fact else 1
    date = event.get("date") or ""
    if date == "Unknown" or not date:
        # Empty / unknown dates sink to the bottom regardless of fact status
        date_key = ""
    else:
        # ISO YYYY-MM-DD sorts lexicographically; invert via maxchar trick so
        # the most recent date sorts first within a fact_rank bucket.
        date_key = "".join(chr(255 - ord(c)) if c.isdigit() else c for c in date)
    return (fact_rank, date_key)


def _source_name_from_url(url: str) -> str:
    """Extract a clean source name from a URL host."""
    if not url:
        return ""
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def build_answers(events: list[dict], topics: list[str]) -> list[dict]:
    """Build per-topic answer objects from accumulated events.

    Args:
        events: List of event dicts with headline, description, topic, date, etc.
        topics: List of topic names requested

    Returns:
        List of answer dicts with validity, confirmation, timing, summary, sources
    """
    answers: list[dict] = []

    for topic in topics:
        topic_events = [e for e in events if e.get("topic") == topic]

        if not topic_events:
            answers.append(
                {
                    "topic": topic,
                    "validity": {
                        "is_valid": False,
                        "statement": "No events found for this topic.",
                        "confidence": "low",
                    },
                    "confirmation": {
                        "is_confirmed": False,
                        "statement": "",
                        "source_name": "",
                        "source_url": "",
                    },
                    "timing": {"happened_at": "", "statement": ""},
                    "summary": "",
                    "valid_sources": [],
                }
            )
            continue

        # Sort: hard-fact events first, then most-recent date within bucket
        sorted_events = sorted(topic_events, key=_sort_key)
        primary = sorted_events[0]

        # Confidence: multi-source + at least one hard fact -> high
        #             single hard-fact source -> medium
        #             only opinion / analysis sources -> low
        hard_facts = [
            e
            for e in sorted_events
            if (e.get("content_type") or "").lower() in ("novel_fact", "report")
        ]
        is_confirmed = bool(hard_facts)
        if is_confirmed and len(sorted_events) >= 2:
            confidence = "high"
        elif is_confirmed:
            confidence = "medium"
        else:
            confidence = "low"

        sources = [
            {
                "title": e.get("headline", ""),
                "source_name": e.get("source_name")
                or _source_name_from_url(e.get("source_url", "")),
                "source_url": e.get("source_url", ""),
                "published_date": e.get("date", ""),
                "supports_claim": True,
            }
            for e in sorted_events
        ]

        primary_date = primary.get("date", "")
        timing_statement = (
            f"Occurred on {primary_date}"
            if primary_date and primary_date != "Unknown"
            else "Date unknown"
        )

        answers.append(
            {
                "topic": topic,
                "validity": {
                    "is_valid": True,
                    "statement": f"Events found for {topic}",
                    "confidence": confidence,
                },
                "confirmation": {
                    "is_confirmed": is_confirmed,
                    "statement": primary.get("description", ""),
                    "source_name": primary.get("source_name")
                    or _source_name_from_url(primary.get("source_url", "")),
                    "source_url": primary.get("source_url", ""),
                },
                "timing": {
                    "happened_at": primary_date,
                    "statement": timing_statement,
                },
                "summary": primary.get("description", ""),
                "valid_sources": sources,
            }
        )

    return answers
