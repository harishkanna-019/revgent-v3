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

    Constructs deterministic trigger-word queries first (guaranteed to
    match how journalists report the event), then supplements with
    LLM-generated queries for diversity. Merges and deduplicates.

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
    current_year = str(datetime.now().year)
    trigger_words = _trigger_words_for_topic(topic)

    # Deterministic queries from trigger words (guaranteed to match event language).
    deterministic = _build_deterministic_queries(company, topic, trigger_words, current_year)

    # Skip LLM query generation when deterministic queries alone fill the budget.
    # This saves a full LLM round-trip (~2s) and is safe because the trigger-word
    # queries already cover the three search angles (direct, action-cluster, temporal).
    if len(deterministic) >= n_queries:
        all_queries = deterministic
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    else:
        # Supplement with LLM-generated queries for diversity.
        llm_queries, usage = await _fetch_llm_queries(
            company, topic, n_queries, trigger_words, current_year, ctx
        )
        all_queries = deterministic + llm_queries

    # Phrase-quote safety net + deduplicate.
    brand_tokens = _brand_phrase_candidates(company)
    all_queries = [_enforce_phrase_quoting(q, brand_tokens) for q in all_queries]
    all_queries = _dedupe_preserving_order(all_queries)

    all_queries = all_queries[:n_queries]

    ctx.record(usage)
    return ToolResult(output=all_queries, usage=usage)


# Common English words that happen to be company names. These brands
# need a disambiguating sector term in search queries or Google returns
# food recipes, gardening tips, and celebrity gossip instead of business news.
_AMBIGUOUS_BRANDS: dict[str, str] = {
    "toast": "restaurant technology",
    "gusto": "payroll HR",
    "notion": "productivity software",
    "linear": "project management software",
    "ramp": "fintech corporate card",
    "brex": "fintech",
    "deel": "HR payroll",
    "lattice": "HR software",
    "gong": "revenue intelligence",
    "clay": "data enrichment",
    "scale": "AI data platform",
    "wiz": "cloud security",
}


def _build_deterministic_queries(
    company: str, topic: str, trigger_words: str, current_year: str
) -> list[str]:
    """Build guaranteed queries from trigger words, no LLM needed.

    Constructs 4 queries covering:
    1. Direct topic match
    2. Trigger word cluster (the key query)
    3. Temporal anchor
    4. Short high-precision variant

    ALWAYS quotes the company name after stripping TLDs — even
    single-word brands like "stripe" or "toast" — because Google
    otherwise matches them as common English words.
    """
    # Strip TLD from domain-format company names
    name = company.lower().strip()
    for tld in (".com", ".io", ".co", ".net", ".org", ".ai", ".app"):
        if name.endswith(tld):
            name = name[: -len(tld)]
            break
    brand = f'"{name}"'

    # Parse trigger words into individual terms for the short variant
    tw_terms = trigger_words.replace('"', "").split(" OR ")
    short_terms = " OR ".join(tw_terms[:4])

    queries = [
        f"{brand} {topic}",
        f"{brand} ({trigger_words})",
        f"{brand} {topic} {current_year}",
        f"{brand} ({short_terms})",
    ]

    # For ambiguous brand names, add a disambiguated query with sector context.
    # "toast" data breach -> '"toast" restaurant technology data breach'
    sector = _AMBIGUOUS_BRANDS.get(name, "")
    if sector:
        queries.append(f"{brand} {sector} {topic}")

    return queries


async def _fetch_llm_queries(
    company: str,
    topic: str,
    n_queries: int,
    trigger_words: str,
    current_year: str,
    ctx: RunContext,
) -> tuple[list[str], dict]:
    """Get LLM-generated queries with trigger words in the prompt."""
    model = ctx.policy.model_for_task("query_generation")
    cache_key = f"queries:{company}:{topic}:{n_queries}:{model}:v3"

    async def _fetch() -> tuple[list[str], dict]:
        prompt = _QUERY_PROMPT.format(
            company=company,
            topic=topic,
            n_queries=n_queries,
            trigger_words=trigger_words,
            current_year=current_year,
        )
        # Cap LLM queries to leave room for deterministic ones
        text, usage_data = await llm.call(
            model=model, max_tokens=512, prompt=prompt
        )
        parsed = _parse_query_list(text)[: n_queries - 4]
        return parsed, usage_data

    return await _query_cache.get_or_compute(cache_key, _fetch)


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
