"""Query generation tool: LLM generates SearXNG-syntax-aware search queries.

Queries are forwarded to Bing News via SearXNG. Operators that pass through:

  - "phrase" quotes        — collapses multi-word brand into a single token
  - (A OR B OR C) booleans — journalist synonyms for the same event type
  - -exclusion             — drops false-positive matches

We seed the LLM with topic-specific "trigger words" — the exact phrases
journalists use to report each event type. This produces queries that match
how the event actually appears in news, rather than broad topic searches.
"""

import json
import re
from datetime import datetime

from cache import AsyncTTLCache
from core.context import RunContext
from core.types import ToolResult
from providers import llm

# Module-level cache: shared across requests for the same (brand, topic).
_query_cache = AsyncTTLCache(ttl_seconds=86400)

# Topic-specific trigger words that journalists use to report each event type.
# These are injected into the query prompt so the LLM generates queries that
# match how these events actually appear in news media.
_TOPIC_TRIGGERS: dict[str, str] = {
    "funding round": (
        '"raises" OR "raised" OR "raising" OR "funding round" OR '
        '"series" OR "valuation" OR "tender offer" OR "secondary" OR '
        '"IPO" OR "investor" OR "capital" OR "financing"'
    ),
    "c-suite executive changes": (
        '"appointed" OR "hires" OR "joins" OR "steps down" OR "resigns" OR '
        '"named" OR "CEO" OR "CFO" OR "CTO" OR "COO" OR "president" OR '
        '"chief" OR "executive" OR "leadership"'
    ),
    "data breach": (
        '"data breach" OR hacked OR breached OR leaked OR ransomware OR '
        '"security incident" OR "customer data" OR "cyberattack" OR '
        '"exposed" OR "stolen" OR "unauthorized access"'
    ),
}


def _trigger_words_for_topic(topic: str) -> str:
    """Return trigger words string for a topic, or a generic fallback."""
    topic_lower = topic.lower().strip()
    for key, triggers in _TOPIC_TRIGGERS.items():
        if key in topic_lower or topic_lower in key:
            return triggers
    # Generic fallback
    words = topic_lower.split()
    if len(words) > 1:
        return " OR ".join(f'"{w}"' for w in words)
    return topic_lower


_QUERY_PROMPT = """You are generating SearXNG-compatible search queries for finding recent news.
The queries are forwarded to Bing News.

Topic-specific trigger words for "{topic}":
{trigger_words}

Rules:
1. ALWAYS wrap multi-word company names in double quotes: "{company}"
2. Use boolean OR clusters with the trigger words above — these are the
   exact phrases journalists use to report this type of event
3. Generate exactly {n_queries} DIVERSE queries. Each must be distinct
   in wording or angle. Spread across three angles:
   - Direct: "{company}" {topic}
   - Action: "{company}" (trigger_word1 OR trigger_word2 OR ...)
   - Temporal: "{company}" {topic} {current_year}
4. Do not use site: operator — it kills recall in our setup.

Return ONLY a JSON array of strings. No prose, no markdown.

Examples:
Company: under armour, Topic: data breach
Queries: ["\\"under armour\\" data breach", "\\"under armour\\" (hacked OR breached OR leaked OR exposed)", "\\"under armour\\" ransomware attack", "\\"under armour\\" customer data stolen", "\\"under armour\\" security incident 2026", "\\"under armour\\" cyberattack"]

Now generate queries for company "{company}", topic "{topic}":"""


async def generate(ctx: RunContext) -> ToolResult:
    """Generate search queries for the current company and topic.

    Calls LLM with topic-specific trigger words baked into the prompt,
    then runs a deterministic post-processor that auto-wraps the brand
    name in quotes for any query that forgot to do so.

    Returns:
        ToolResult with output=list[str], capped at policy.max_queries_per_topic.
    """
    company = ctx.company.strip().lower() if ctx.company else ""
    topic = (
        ctx.topic.simplified.strip().lower()
        if ctx.topic and ctx.topic.simplified
        else (ctx.topic.original.strip().lower() if ctx.topic else "")
    )

    if not company or not topic:
        return ToolResult(output=[f"{company} {topic}".strip()])

    n_queries = ctx.policy.max_queries_per_topic
    model = ctx.policy.model_for_task("query_generation")
    current_year = str(datetime.now().year)
    trigger_words = _trigger_words_for_topic(topic)

    # Cache key includes trigger words so different topics get fresh queries
    cache_key = f"queries:{company}:{topic}:{n_queries}:{model}:v2"

    async def _fetch() -> tuple[list[str], dict]:
        prompt = _QUERY_PROMPT.format(
            company=company,
            topic=topic,
            n_queries=n_queries,
            trigger_words=trigger_words,
            current_year=current_year,
        )
        text, usage = await llm.call(model=model, max_tokens=512, prompt=prompt)
        parsed = _parse_query_list(text)
        return parsed, usage

    queries, usage = await _query_cache.get_or_compute(cache_key, _fetch)

    # Deterministic safety net: always phrase-quote the brand.
    brand_tokens = _brand_phrase_candidates(company)
    queries = [_enforce_phrase_quoting(q, brand_tokens) for q in queries]

    queries = _dedupe_preserving_order(queries)

    if not queries:
        if " " in company:
            queries = [f'"{company}" {topic}']
        else:
            queries = [f"{company} {topic}"]

    queries = queries[:n_queries]

    ctx.record(usage)
    return ToolResult(output=queries, usage=usage)


# ── Helpers ──


def _brand_phrase_candidates(company: str) -> list[str]:
    """Return multi-word brand strings that must be phrase-quoted."""
    cleaned = company.lower().strip()
    for tld in (".com", ".io", ".co", ".net", ".org", ".ai", ".app"):
        if cleaned.endswith(tld):
            cleaned = cleaned[: -len(tld)]
    cleaned = cleaned.strip()
    if " " in cleaned:
        return [cleaned]
    return []


def _enforce_phrase_quoting(query: str, brand_candidates: list[str]) -> str:
    """Wrap any multi-word brand mention in double quotes if not already quoted."""
    if not query or not brand_candidates:
        return query.strip()

    result = query
    for brand in brand_candidates:
        if not brand or " " not in brand:
            continue
        pattern = re.compile(
            r'(?<!["\w])' + re.escape(brand) + r'(?!["\w])',
            re.IGNORECASE,
        )
        result = pattern.sub(f'"{brand}"', result)

    return result.strip()


def _dedupe_preserving_order(queries: list[str]) -> list[str]:
    """Remove duplicates while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        if not q:
            continue
        norm = " ".join(q.lower().split())
        if norm and norm not in seen:
            seen.add(norm)
            out.append(q.strip())
    return out


def _parse_query_list(text: str) -> list[str]:
    """Parse JSON array of query strings from LLM response."""
    text = text.strip()

    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("["):
                text = part
                break

    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

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

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    seen = set()
    result = []
    for line in lines:
        cleaned = line.lstrip("-").lstrip("*").lstrip("\u2022").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result
