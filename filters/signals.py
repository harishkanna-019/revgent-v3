"""Signal routing filter — classifies validated results into lanes.

Pure logic, no I/O. Routes:
- valid + hard_fact → event lane
- valid + soft/opinion → signal lane (with signal_type + confidence)
- invalid → discard lane
"""

from dataclasses import dataclass

from formatting import format_event


@dataclass(frozen=True)
class LaneDecision:
    """Routing decision for a validated result."""

    lane: str  # "event" | "signal" | "discard"
    event: dict | None = None
    signal: dict | None = None


# Signal type inference keywords (order matters — first match wins)
_SIGNAL_KEYWORDS = [
    (
        "market_speculation",
        [
            "speculation",
            "speculative",
            "rumor",
            "rumored",
            "might ",
            "could ",
            "may ",
            "potential ",
            "possibly",
            "speculated",
        ],
    ),
    (
        "unconfirmed",
        [
            "unconfirmed",
            "reportedly",
            "allegedly",
            "claimed",
            "sources say",
            "according to sources",
            "unverified",
        ],
    ),
    ("early_report", ["early", "preliminary", "initial", "breaking", "developing"]),
    (
        "analyst_commentary",
        [
            "analyst",
            "expert",
            "commentary",
            "opinion",
            "editorial",
            "viewpoint",
            "perspective",
        ],
    ),
]


def _infer_signal_type(fact_check_raw: str) -> str:
    """Infer signal type from fact-check raw text."""
    text = fact_check_raw.lower()
    for signal_type, keywords in _SIGNAL_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return signal_type
    # Default fallback
    return "analyst_commentary"


def _confidence_score(is_valid: bool, is_hard_fact: bool, fact_check_raw: str) -> float:
    """Calculate confidence score for a signal (0.0–1.0)."""
    if not is_valid:
        return 0.0
    if is_hard_fact:
        return 1.0

    # Soft signals: base confidence, adjusted by signal type
    text = fact_check_raw.lower()
    base = 0.6

    # Boost for strong language
    strong_markers = ["definitely", "certainly", "confirmed by", "sources confirm"]
    if any(m in text for m in strong_markers):
        base += 0.15

    # Penalty for weak language
    weak_markers = ["might", "could", "possibly", "maybe", "unclear"]
    if any(m in text for m in weak_markers):
        base -= 0.15

    return round(max(0.3, min(0.9, base)), 2)


def classify_result(
    result: dict,
    is_valid: bool,
    is_hard_fact: bool,
    fact_check_raw: str,
    topic: str,
) -> LaneDecision:
    """Route a validated result to the correct lane.

    Args:
        result: The original search result dict (title, url, content, published_date)
        is_valid: True if relevance check passed
        is_hard_fact: True if fact check says hard fact
        fact_check_raw: Raw text from the fact-check LLM call
        topic: Current topic name

    Returns:
        LaneDecision with lane="event" | "signal" | "discard"
    """
    if not is_valid:
        return LaneDecision(lane="discard")

    if is_hard_fact:
        # Route to event lane
        event = format_event(result, content_type="novel_fact")
        event["topic"] = topic
        return LaneDecision(lane="event", event=event)

    # Route to signal lane
    signal_type = _infer_signal_type(fact_check_raw)
    confidence = _confidence_score(is_valid, is_hard_fact, fact_check_raw)

    signal = {
        "headline": result.get("title", ""),
        "description": result.get("content", "")[:400],
        "topic": topic,
        "date": result.get("published_date", "Unknown"),
        "source_name": _extract_source_name(result.get("url", "")),
        "source_url": result.get("url", ""),
        "signal_type": signal_type,
        "confidence": confidence,
        "why_not_event": f"Classified as {signal_type} (soft signal, not hard fact)",
        "cost_attribution": 0.0,
    }

    return LaneDecision(lane="signal", signal=signal)


def _extract_source_name(url: str) -> str:
    """Extract domain from URL as source name."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url
