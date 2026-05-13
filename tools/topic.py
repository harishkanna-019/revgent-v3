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

_KEYWORD_PROMPT = """List 10-18 short, GENERIC keywords that journalists commonly use
when writing about the topic below. These keywords are used to check whether
an article is actually about this topic - they're a coarse precision gate,
not the final filter. Recall here is more important than precision because
the LLM validation stage downstream rejects false matches.

Topic: {topic}

Rules:
- Do NOT include the company name in any keyword. "layoffs" is good;
  "acme corp layoffs" is bad.
- INCLUDE the core single-word root of the topic on its own. If the topic
  is "strategic partnerships", the list MUST include "partnership" as a
  standalone keyword. Multi-word phrases like "strategic partnership" miss
  articles that say "Empire Partnership" or "announced a partnership".
  The single-word root catches both; phrases catch only exact matches.
- Each keyword must be a literal phrase a news article would actually contain
  ("cut jobs", "axed", "workforce reduction"), not a query ("acme layoffs news").
- Cover synonyms, related actions, and adjacent industry vocabulary.
- Use 1-2 word keywords; the keyword matcher does prefix matching so
  "layoff" hits "layoffs", "hack" hits "hacked" / "hacker" / "hacking".
- Return ONLY a JSON array of lowercase strings.

Examples (note that each list has the single-word root + variations + synonyms):
- Topic "layoffs" -> ["layoff", "laid off", "job cuts", "cut jobs", "workforce reduction", "headcount", "restructuring", "downsize", "redundancies", "hiring freeze", "reduce staff", "axed", "cost cutting", "rightsizing", "fire", "fired", "firings"]
- Topic "earnings" -> ["earnings", "revenue", "profit", "loss", "quarterly results", "q1", "q2", "q3", "q4", "financial results", "ebitda", "guidance", "missed estimates", "beat estimates", "eps"]
- Topic "strategic partnerships" -> ["partnership", "partnered", "alliance", "joint venture", "jv", "collaboration", "agreement", "deal", "teaming up", "joining forces", "co-develop", "mou", "memorandum", "strategic alliance", "distribution deal"]
- Topic "new product launches" -> ["launch", "launched", "unveiled", "debut", "introduces", "rolled out", "rollout", "announcement", "release", "new product", "new feature", "beta", "now available", "reveal"]
- Topic "data breaches" -> ["breach", "breached", "hack", "hacked", "leak", "leaked", "exposed", "compromised", "cyberattack", "ransomware", "phishing", "data leak", "data theft", "stolen data", "customer data", "records exposed", "security incident"]
- Topic "funding" -> ["funding", "raised", "series a", "series b", "series c", "seed", "valuation", "investors", "venture capital", "investment", "round", "raise"]
- Topic "executive changes" -> ["ceo", "cfo", "cto", "chief", "president", "appointed", "steps down", "resigns", "departure", "hires", "named", "new ceo", "executive", "leadership change"]
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
        simplify_usage: dict = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
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
    # Keywords are now topic-only (no company prefix), so the cache key is
    # global across companies and depths. Massive cost saving on batches.
    cache_key = f"keywords:{simplified}"

    async def _fetch_keywords() -> tuple[list[str], dict]:
        model = ctx.policy.model_for_task("keyword_generation")
        prompt = _KEYWORD_PROMPT.format(topic=simplified)
        text, usage = await llm.call(model=model, max_tokens=192, prompt=prompt)

        keywords = _parse_keyword_list(text)
        return keywords, usage

    keywords, keyword_usage = await _keyword_cache.get_or_compute(
        cache_key, _fetch_keywords
    )

    # ── Combine usage ──
    total_usage = {
        "input_tokens": simplify_usage.get("input_tokens", 0)
        + keyword_usage.get("input_tokens", 0),
        "output_tokens": simplify_usage.get("output_tokens", 0)
        + keyword_usage.get("output_tokens", 0),
        "total_tokens": simplify_usage.get("total_tokens", 0)
        + keyword_usage.get("total_tokens", 0),
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
        text = text[start : end + 1]

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
    words = [
        w.strip().lower() for w in text.replace(",", "\n").split("\n") if w.strip()
    ]
    seen = set()
    result = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result
