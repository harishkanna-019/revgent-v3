"""Build validated answer objects from events. Pure transforms."""


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
            answers.append({
                "topic": topic,
                "validity": {"is_valid": False, "statement": "No events found for this topic.", "confidence": "low"},
                "confirmation": {"is_confirmed": False, "statement": "", "source_name": "", "source_url": ""},
                "timing": {"happened_at": "", "statement": ""},
                "summary": "",
                "valid_sources": [],
            })
            continue

        # Use the highest-ranked event as the primary source
        primary = topic_events[0]
        sources = [
            {
                "title": e.get("headline", ""),
                "source_name": e.get("source_name", ""),
                "source_url": e.get("source_url", ""),
                "published_date": e.get("date", ""),
                "supports_claim": True,
            }
            for e in topic_events
        ]

        answers.append({
            "topic": topic,
            "validity": {
                "is_valid": True,
                "statement": f"Events found for {topic}",
                "confidence": "high",
            },
            "confirmation": {
                "is_confirmed": True,
                "statement": primary.get("description", ""),
                "source_name": primary.get("source_name", ""),
                "source_url": primary.get("source_url", ""),
            },
            "timing": {
                "happened_at": primary.get("date", ""),
                "statement": f"Occurred on {primary.get('date', '')}",
            },
            "summary": primary.get("description", ""),
            "valid_sources": sources,
        })

    return answers
