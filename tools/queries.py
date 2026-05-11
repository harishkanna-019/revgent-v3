"""Query generation tool: LLM generates search queries for company + topic."""

import json

from core.context import RunContext
from core.types import ToolResult
from providers import llm

_QUERY_PROMPT = """Generate 7-10 diverse search query strings for finding recent news and information about {company} related to: {topic}

Return ONLY a JSON array of strings. Each query should be a natural search phrase.
Examples:
- ["meta layoffs 2026", "meta platforms job cuts", "facebook workforce reduction", "meta restructuring news"]
- ["apple earnings Q1 2026", "apple revenue report", "apple financial results latest"]

Company: {company}
Topic: {topic}
"""


async def generate(ctx: RunContext) -> ToolResult:
    """Generate search queries for the current company and topic.

    Uses ctx.company and ctx.topic.simplified (falls back to original).
    Calls LLM to produce 7-10 query strings. Parses JSON array from response.
    Falls back to ["{company} {topic}"] on parse failure.

    Returns:
        ToolResult with output=list[str]
    """
    company = ctx.company.strip().lower() if ctx.company else ""
    topic = (
        ctx.topic.simplified.strip().lower()
        if ctx.topic and ctx.topic.simplified
        else (ctx.topic.original.strip().lower() if ctx.topic else "")
    )

    if not company or not topic:
        return ToolResult(output=[f"{company} {topic}".strip()])

    model = ctx.policy.model_for_task("query_generation")
    prompt = _QUERY_PROMPT.format(company=company, topic=topic)
    text, usage = await llm.call(model=model, max_tokens=256, prompt=prompt)

    queries = _parse_query_list(text)

    if not queries:
        queries = [f"{company} {topic}"]

    # Limit to max_queries_per_topic from policy
    max_queries = ctx.policy.max_queries_per_topic
    queries = queries[:max_queries]

    # Record cost
    ctx.record(usage)

    return ToolResult(output=queries, usage=usage)


def _parse_query_list(text: str) -> list[str]:
    """Parse JSON array of query strings from LLM response.

    Handles markdown code blocks, extra text, deduplication.
    """
    text = text.strip()

    # Extract from markdown code block
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                text = part
                break

    # Find JSON array bounds
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    try:
        queries = json.loads(text)
        if isinstance(queries, list):
            seen: set[str] = set()
            result: list[str] = []
            for q in queries:
                if isinstance(q, str):
                    query = q.strip()
                    if query and query.lower() not in seen:
                        seen.add(query.lower())
                        result.append(query)
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: split by newlines, filter empty
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    seen = set()
    result = []
    for line in lines:
        # Remove common bullet/list markers
        cleaned = line.lstrip("-").lstrip("*").lstrip("•").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result
