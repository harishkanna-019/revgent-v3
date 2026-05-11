"""Formatting tool: summary + classification via concurrent LLM calls."""

import asyncio

from core.context import RunContext
from core.types import ToolResult
from formatting import format_event
from providers import llm

_SUMMARY_PROMPT = """Summarize the following article in 2-3 sentences. Focus on the key facts and main point.

Title: {title}
Content: {content}

Summary:"""

_CLASSIFICATION_PROMPT = """Classify the following article into one content type:

Title: {title}
Content: {content}

Content types:
- novel_fact: A new, previously unreported factual development
- report: A factual summary of recent events or data
- analysis: Interpretation, commentary, or analytical breakdown
- historical: Background context or historical overview

Answer with exactly one word: novel_fact, report, analysis, or historical."""

_VALID_TYPES = {"novel_fact", "report", "analysis", "historical"}


def _parse_classification(text: str) -> str:
    """Parse classification response into a valid content type."""
    text = text.strip().lower()
    for t in _VALID_TYPES:
        if t in text:
            return t
    return "analysis"


def _merge_usage(u1: dict, u2: dict) -> dict:
    """Merge two usage dicts."""
    return {
        "input_tokens": u1.get("input_tokens", 0) + u2.get("input_tokens", 0),
        "output_tokens": u1.get("output_tokens", 0) + u2.get("output_tokens", 0),
        "total_tokens": u1.get("total_tokens", 0) + u2.get("total_tokens", 0),
    }


async def _summarize(model: str, title: str, content: str) -> tuple[str, dict]:
    """Call LLM for article summary."""
    prompt = _SUMMARY_PROMPT.format(title=title, content=content[:4000])
    text, usage = await llm.call(model=model, max_tokens=256, prompt=prompt)
    return text.strip(), usage


async def _classify(model: str, title: str, content: str) -> tuple[str, dict]:
    """Call LLM for content type classification."""
    prompt = _CLASSIFICATION_PROMPT.format(title=title, content=content[:2000])
    text, usage = await llm.call(model=model, max_tokens=32, prompt=prompt)
    return _parse_classification(text), usage


async def format_one(ctx: RunContext, candidate: dict) -> ToolResult:
    """Format a candidate into an event dict.

    Concurrently calls LLM for:
    1. Summary of the article
    2. Content type classification (novel_fact, report, analysis, historical)

    Args:
        ctx: RunContext with policy
        candidate: Search result dict with title, url, content, published_date

    Returns:
        ToolResult with output=event_dict (format_event shape)
    """
    title = candidate.get("title", "")
    content = candidate.get("content", "")
    url = candidate.get("url", "")
    item_id = url or title[:50]

    model = ctx.policy.model_for_task("summarization")

    # ── Concurrent LLM calls ──
    summary_task = asyncio.create_task(_summarize(model, title, content))
    classification_task = asyncio.create_task(_classify(model, title, content))

    summary, summary_usage = await summary_task
    content_type, classification_usage = await classification_task

    total_usage = _merge_usage(summary_usage, classification_usage)

    # Build event dict
    event = format_event(candidate, summary=summary, content_type=content_type)
    event["cost_attribution"] = 0.0  # Will be set by caller if needed

    ctx.record(total_usage, item_id=item_id)

    return ToolResult(output=event, usage=total_usage, item_id=item_id)
