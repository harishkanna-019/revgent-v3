"""Topic analysis tool: simplification + keyword extraction via LLM. Cached 24h."""

import json

from cache import AsyncTTLCache
from core.context import RunContext
from core.types import ToolResult
from providers import llm

# Module-level cache (24h TTL for keywords)
_keyword_cache = AsyncTTLCache(ttl_seconds=86400)

_SIMPLIFY_PROMPT = """Simplify the following research topic into 1-3 concise words that capture the core concept.

Topic: {topic}

Return ONLY the simplified topic as a short phrase (no quotes, no explanation).
Examples:
- "recent layoffs at meta" → layoffs
- "new product launches and announcements" → product launches
- "earnings report Q1 2026" → earnings
"""

_KEYWORD_PROMPT = """Extract 5-10 relevant search keywords for finding news articles about {company} related to: {topic}

Return ONLY a JSON array of strings. Examples:
- ["layoffs", "job cuts", "workforce reduction", "hiring freeze", " restructuring"]
- ["earnings", "revenue", "profit", "quarterly results", "financial report"]
"""


async def analyze(ctx: RunContext) -> ToolResult:
    """Analyze a topic: simplify and extract keywords.

    Uses the current topic from ctx.topic.original.
    If the topic is ≤3 words, simplification is skipped (original = simplified).
    Keywords are always generated via LLM and cached 24h.

    Returns:
        ToolResult with output={"simplified": str, "keywords": list[str]}
    """
    topic = ctx.topic.original if ctx.topic else ""
    if not topic:
        return ToolResult(output={"simplified": "", "keywords": []})

    # ── Simplification ──
    words = topic.strip().split()
    if len(words) <= 3:
        simplified = topic.strip().lower()
        simplify_usage: dict = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    else:
        model = ctx.policy.model_for_task("topic_simplification")
        prompt = _SIMPLIFY_PROMPT.format(topic=topic)
        simplify_text, simplify_usage = await llm.call(
            model=model, max_tokens=32, prompt=prompt
        )
        simplified = simplify_text.strip().lower().strip('"').strip("'")
        if not simplified:
            simplified = topic.strip().lower()

    # ── Keywords ──
    company = ctx.company.strip().lower()
    cache_key = f"keywords:{company}:{simplified}"

    async def _fetch_keywords() -> tuple[list[str], dict]:
        model = ctx.policy.model_for_task("keyword_generation")
        prompt = _KEYWORD_PROMPT.format(company=company, topic=simplified)
        text, usage = await llm.call(model=model, max_tokens=128, prompt=prompt)

        keywords = _parse_keyword_list(text)
        return keywords, usage

    keywords, keyword_usage = await _keyword_cache.get_or_compute(cache_key, _fetch_keywords)

    # ── Combine usage ──
    total_usage = {
        "input_tokens": simplify_usage.get("input_tokens", 0) + keyword_usage.get("input_tokens", 0),
        "output_tokens": simplify_usage.get("output_tokens", 0) + keyword_usage.get("output_tokens", 0),
        "total_tokens": simplify_usage.get("total_tokens", 0) + keyword_usage.get("total_tokens", 0),
    }

    # Record cost on context
    ctx.record(total_usage)

    return ToolResult(
        output={"simplified": simplified, "keywords": keywords},
        usage=total_usage,
    )


def _parse_keyword_list(text: str) -> list[str]:
    """Parse JSON array of keywords from LLM response.

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
        keywords = json.loads(text)
        if isinstance(keywords, list):
            seen: set[str] = set()
            result: list[str] = []
            for kw in keywords:
                if isinstance(kw, str):
                    k = kw.strip().lower()
                    if k and k not in seen:
                        seen.add(k)
                        result.append(k)
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: split by commas or newlines
    words = [w.strip().lower() for w in text.replace(",", "\n").split("\n") if w.strip()]
    seen = set()
    result = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result
