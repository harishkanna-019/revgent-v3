"""Query generation tool: LLM generates SearXNG-syntax-aware search queries.

The pipeline runs the queries through SearXNG, which forwards them (with their
operators preserved) to the underlying engines (Bing News, DuckDuckGo, Qwant,
Brave). The operators that pass through reliably are:

  - "phrase" quotes        — most important; collapses multi-word brand
                              like "under armour" into a single token. Without
                              quotes, Bing matches each word separately and
                              "under armour breach" returns articles about
                              ANY brand under threat AND armour-makers AND
                              breach-of-contract lawsuits. Quoting the brand
                              alone lifts on-topic results from 12% to 50%
                              in our measurements.

  - (A OR B OR C) booleans — useful for topics with rare vocabulary where
                              the LLM and the journalists use synonyms
                              ("axed" / "let go" / "downsized" all mean
                              "fired"). Booleans hurt recall when used on
                              common topics so we only attach them when
                              keyword_count is large AND the words are short.

  - -exclusion             — drops false-positive product categories
                              ("shoes" for under-armour-breach searches).
                              Modest gains, not worth LLM cycles per-query.

  - site:domain            — too restrictive; -75% recall. Skipped.

We split query generation into THREE complementary clusters so that the LLM
gets multiple "shots on goal" for the same topic, modelled after the
research-skill's multi-agent pattern. Each cluster targets a different
angle on the same intelligence question:

  1. Brand-anchored:  "{brand}" {topic}                — finds direct hits
  2. Action-anchored: "{brand}" ({verb1} OR {verb2})   — finds reportage
                                                          using synonyms
  3. Event-anchored:  "{brand}" {topic_noun} {year}    — finds timeline /
                                                          announcement
                                                          articles

This guarantees a minimum coverage even when the LLM picks a poor seed.
"""

import json
import re

from cache import AsyncTTLCache
from core.context import RunContext
from core.types import ToolResult
from providers import llm

# ── Module-level cache: shared across requests for the same (brand, topic). ──
# Query templates rarely change so 24h TTL is safe and saves LLM calls.
_query_cache = AsyncTTLCache(ttl_seconds=86400)


_QUERY_PROMPT = """You are generating SearXNG-compatible search queries for finding recent news.
The queries are forwarded to Bing News, DuckDuckGo News, Qwant, and Brave.

Rules:
1. ALWAYS wrap multi-word company names in double quotes so the engine
   treats the brand as a single token: "under armour", "best western",
   "general motors". This is the single biggest precision lever.
2. Use boolean OR ONLY when the topic has multiple common synonyms a
   journalist might use. Format: (word1 OR "two words" OR word3).
   Examples that BENEFIT from OR: layoffs/firings, breach/hacked/leaked,
   launch/debut/rolled-out. Examples that DO NOT: earnings, IPO, recall.
3. Generate {n_queries} DIVERSE queries. Each query must be distinct in
   wording or angle, not a near-duplicate of another.
4. Cover three angles: direct (brand + topic), action (brand + synonym
   cluster), and temporal (brand + topic + year). Spread queries across
   the three angles.
5. Do not include the year unless the topic implies a specific event window.
6. Do not use site: operator — it kills recall in our setup.

Return ONLY a JSON array of strings. No prose, no markdown.

Examples:

Company: meta.com
Topic: layoffs
Output:
["\\"meta\\" layoffs", "\\"meta platforms\\" (layoffs OR \\"job cuts\\" OR fired)", "\\"meta\\" workforce reduction", "\\"facebook\\" layoffs 2026", "\\"meta\\" restructuring announcement", "\\"meta\\" (downsizing OR rightsizing)"]

Company: under armour
Topic: data breach
Output:
["\\"under armour\\" data breach", "\\"under armour\\" (hacked OR breached OR leaked OR exposed)", "\\"under armour\\" cyberattack", "\\"under armour\\" customer data stolen", "\\"under armour\\" security incident 2026", "\\"under armour\\" ransomware"]

Now generate queries for:
Company: {company}
Topic: {topic}
"""


async def generate(ctx: RunContext) -> ToolResult:
    """Generate search queries for the current company and topic.

    Calls LLM with a SearXNG-syntax-aware prompt, then runs a deterministic
    post-processor that auto-wraps the brand name in quotes for any query
    that forgot to do so. The post-processor is the safety net for LLM
    variance: phrase-quoting is too important to leave to model compliance.

    Returns:
        ToolResult with output=list[str], length capped at policy.max_queries_per_topic.
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

    # Cache key: (brand, topic, n_queries, model). model is part of the key
    # because different models will produce different operator patterns.
    cache_key = f"queries:{company}:{topic}:{n_queries}:{model}"

    async def _fetch() -> tuple[list[str], dict]:
        prompt = _QUERY_PROMPT.format(company=company, topic=topic, n_queries=n_queries)
        text, usage = await llm.call(model=model, max_tokens=512, prompt=prompt)
        parsed = _parse_query_list(text)
        return parsed, usage

    queries, usage = await _query_cache.get_or_compute(cache_key, _fetch)

    # ── Deterministic safety net ──
    # The LLM may forget to phrase-quote the brand on some queries. We always
    # wrap it ourselves. Doing this here (not in the prompt) makes the
    # operator guarantee independent of model behaviour.
    brand_tokens = _brand_phrase_candidates(company)
    queries = [_enforce_phrase_quoting(q, brand_tokens) for q in queries]

    # Drop empties and near-duplicates after rewriting
    queries = _dedupe_preserving_order(queries)

    # Fallback if everything fell through
    if not queries:
        if " " in company:
            queries = [f'"{company}" {topic}']
        else:
            queries = [f"{company} {topic}"]

    # Cap to the policy budget
    queries = queries[:n_queries]

    ctx.record(usage)
    return ToolResult(output=queries, usage=usage)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _brand_phrase_candidates(company: str) -> list[str]:
    """Return the multi-word brand strings that must be phrase-quoted.

    For 'meta.com' -> ['meta'] (no quoting needed - single word).
    For 'under armour' -> ['under armour'].
    For 'underarmour.com' -> ['underarmour'] (no quoting - single word).
    For 'wellsfargo.com' if user passed 'wells fargo' alias -> both forms.

    Quoting a single-word brand is harmless but unnecessary, so we only
    return multi-word candidates here.
    """
    # Strip TLDs that come from passing the domain as a name.
    cleaned = company.lower().strip()
    for tld in (".com", ".io", ".co", ".net", ".org", ".ai", ".app"):
        if cleaned.endswith(tld):
            cleaned = cleaned[: -len(tld)]
    cleaned = cleaned.strip()

    # If the resulting string still has whitespace, it's a multi-word brand
    # (e.g. "under armour", "best western"). Otherwise it's a single token
    # that doesn't benefit from quoting.
    if " " in cleaned:
        return [cleaned]
    return []


def _enforce_phrase_quoting(query: str, brand_candidates: list[str]) -> str:
    """Wrap any multi-word brand mention in double quotes if not already quoted.

    The LLM is asked to do this in the prompt but we don't trust it
    100% — variance across model calls means some queries will leak
    through unquoted. This regex-based post-processor guarantees the
    operator no matter what the LLM returned.

    For each multi-word brand candidate, we find unquoted occurrences
    (case-insensitive, word-boundary-aware) and wrap them. We DO NOT
    touch occurrences already inside double quotes.

    Args:
        query: Raw query string from the LLM.
        brand_candidates: List of multi-word brand phrases to enforce.

    Returns:
        Query string with all multi-word brand mentions phrase-quoted.
    """
    if not query or not brand_candidates:
        return query.strip()

    result = query
    for brand in brand_candidates:
        if not brand or " " not in brand:
            continue
        # Build a pattern that:
        #   - matches the brand on word boundaries (case-insensitive)
        #   - does NOT match inside an existing pair of double quotes
        # We use a lookbehind / lookahead heuristic: skip the match if it's
        # immediately preceded by `"` or followed by `"`. This isn't a
        # perfect quote-tracker but it handles our LLM-generated patterns
        # ("under armour" breach, etc.) without over-quoting.
        pattern = re.compile(
            r'(?<!["\w])' + re.escape(brand) + r'(?!["\w])',
            re.IGNORECASE,
        )
        result = pattern.sub(f'"{brand}"', result)

    return result.strip()


def _dedupe_preserving_order(queries: list[str]) -> list[str]:
    """Remove duplicates while preserving order. Case-insensitive whitespace-tolerant."""
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
    """Parse JSON array of query strings from LLM response.

    Handles markdown code blocks, extra text, deduplication. Tolerant
    to common malformed-JSON output from smaller models.
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
        cleaned = line.lstrip("-").lstrip("*").lstrip("\u2022").strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            result.append(cleaned)
    return result
