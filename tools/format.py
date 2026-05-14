"""Formatting tool: combined summary + classification in one LLM call."""

from core.context import RunContext
from core.types import ToolResult
from formatting import format_event
from providers import llm

_FORMAT_PROMPT = """Summarize this article in 2-3 sentences focusing on the key facts, then classify it.

Title: {title}
Content: {content}

Respond in EXACTLY this format (two lines):
SUMMARY: <your 2-3 sentence summary>
TYPE: <one of: novel_fact, report, analysis, historical>

Type definitions:
- novel_fact: A new, previously unreported factual development
- report: A factual summary of recent events or data
- analysis: Interpretation, commentary, or analytical breakdown
- historical: Background context or historical overview"""

_VALID_TYPES = {"novel_fact", "report", "analysis", "historical"}


def _parse_format_response(text: str) -> tuple[str, str]:
    """Parse combined summary+classification response.

    Returns (summary, content_type).
    """
    summary = ""
    content_type = "analysis"

    for line in text.strip().split("\n"):
        line = line.strip()
        if line.upper().startswith("SUMMARY:"):
            summary = line[len("SUMMARY:"):].strip()
        elif line.upper().startswith("TYPE:"):
            raw_type = line[len("TYPE:"):].strip().lower()
            for t in _VALID_TYPES:
                if t in raw_type:
                    content_type = t
                    break

    # Fallback: if no SUMMARY: prefix found, use the whole text as summary
    if not summary:
        lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
        # Filter out the TYPE line
        non_type = [l for l in lines if not l.upper().startswith("TYPE:")]
        summary = " ".join(non_type)[:500]

    return summary, content_type


async def format_one(ctx: RunContext, candidate: dict) -> ToolResult:
    """Format a candidate into an event dict.

    Single LLM call that produces both a summary and content type
    classification, halving the token cost vs two separate calls.

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

    prompt = _FORMAT_PROMPT.format(title=title, content=content[:4000])
    text, usage = await llm.call(model=model, max_tokens=300, prompt=prompt)

    summary, content_type = _parse_format_response(text)

    event = format_event(candidate, summary=summary, content_type=content_type)
    event["topic"] = (
        ctx.topic.original
        if ctx.topic is not None and ctx.topic.original
        else (ctx.topics[-1] if ctx.topics else "")
    )
    event["cost_attribution"] = 0.0

    ctx.record(usage, item_id=item_id)

    return ToolResult(output=event, usage=usage, item_id=item_id)
