# SearXNG Search Syntax — What Works, What Doesn't

Findings from operator-passthrough testing against the production SearXNG
deployment (Bing News, DuckDuckGo, Qwant, Brave engines, news category).
Tested May 2026.

## TL;DR

| Operator | Works through SearXNG? | Use in our pipeline? |
|---|---|---|
| `"phrase quotes"` | Yes — critical | Yes, every multi-word brand |
| `(A OR B OR C)` Boolean | Yes — useful for synonym-rich topics | Yes, LLM decides per topic |
| `-exclude` | Yes, partial — reorders results | No — wasted query budget |
| `site:domain.com` | Yes but **kills recall** (~75% drop) | No |
| `!engine` engine bang | Yes for some engines | No — locks us to one engine |
| `after:` / `before:` | Inconsistent — engines may ignore | No — we use `time_range` instead |

The single biggest precision lever is **phrase-quoting multi-word brand
names**. Unquoted `"under armour" breach` returns 0/10 on-topic results
because Bing matches each word separately ("under threat", "body armour",
etc). Quoted `"under armour" breach` returns 5/10 on-topic — a 5× lift.

## Methodology

We probed the production SearXNG endpoint (`searxng.railway.internal:8080`)
with a battery of test queries varying one operator at a time and counted:

1. Total results returned
2. Results actually mentioning the target brand (on-topic vs noise)
3. Date-tagged vs Unknown-date result ratio
4. Result diversity (unique domains)

All tests used `categories=news` and `time_range=year`, matching how the
pipeline calls SearXNG in production.

## Operator-by-operator findings

### `"phrase quotes"` — CRITICAL

Wrapping a multi-word brand in quotes forces the engine to treat it as
a single token instead of two free-floating words. The impact is large:

| Brand | Unquoted on-topic | Quoted on-topic |
|---|---|---|
| best buy | 1/8 | **5/10** |
| best western | 2/10 | **3/7** |
| under armour | 0/10 | (lower recall but cleaner) |

Single-word brands ("meta", "twitch", "duolingo") don't need quoting.

**Implementation:** `tools/queries.py` runs every LLM-generated query
through `_enforce_phrase_quoting()` which wraps any unquoted occurrence
of a multi-word brand. The LLM is also instructed to quote in the prompt;
the post-processor is a deterministic safety net for model variance.

### `(A OR B OR C)` Boolean — Conditional

Boolean OR groups help when a topic has multiple common synonyms that
journalists actually use:

- **Helps:** `layoffs OR "job cuts" OR fired OR "workforce reduction"`
- **Helps:** `breach OR hacked OR leaked OR exposed`
- **Hurts (over-broadens):** `launch OR debut OR rollout` on `duolingo`
  reduced results from 10 → 7 because the wide vocabulary matched
  off-topic finance news

The LLM is prompted to use OR only on topics with rare vocabulary. We
don't enforce this in code — model judgment is usually correct.

### `-exclude` — Skip

We tested `meta layoffs -site:reddit.com -site:facebook.com -site:youtube.com`
against `meta layoffs`. Result domains were nearly identical because
SearXNG's news category already filters out social-media domains
upstream. The `-` operator just wastes the LLM's query-token budget.

### `site:domain.com` — Don't use

Restricting to a specific publication is appealing ("only show me
Reuters") but the news engines have small indexes per-domain. Testing:

```
"under armour" breach site:bleepingcomputer.com    → 0 results
"under armour" breach site:reuters.com             → 0 results
```

A 75% recall drop. The credibility-domain bonus in `filters/ranker.py`
is the better place to bias toward credible sources — it boosts them
in ranking without filtering anything out at search time.

### `!engine` engine bang — Don't lock in

SearXNG accepts `!bing_news`, `!duckduckgo`, etc. to restrict to one
underlying engine. Useful for debugging but not for the pipeline —
different engines have different recency and different date metadata
formats, and we want the union, not the intersection.

### `after:` / `before:` date operators — Engines may ignore

`after:2026-01-01` passes through but the underlying engines (especially
Bing News) don't always honor it. The `time_range` parameter SearXNG
sets internally is much more reliable.

## Pipeline strategy

Our query generator (`tools/queries.py`) follows a three-angle pattern
inspired by the research-skill's multi-agent approach. Each query targets
a different angle on the same intelligence question:

1. **Direct:** `"{brand}" {topic}` — finds straightforward news hits
2. **Action cluster:** `"{brand}" ({verb1} OR {verb2} OR {verb3})` —
   catches synonym reportage
3. **Temporal:** `"{brand}" {topic} {year}` — catches dated
   announcements

This gives the pipeline multiple shots on goal. Even when one angle
returns nothing (e.g. the LLM picked an unusual synonym cluster), the
other angles still hit.

### Depth-aware query budget

| Depth | Queries per topic | Rationale |
|---|---|---|
| cheap | 2 | Smoke-test: brand + first synonym |
| standard | 8 | Full three-angle coverage |
| deep | 12 | Adds long-tail vocabulary variants |

The LLM is told the budget in the prompt, so it can pick the most
diverse 2 / 8 / 12 queries instead of always producing 8 and getting
truncated.

## Topic keyword strategy

`tools/topic.py` generates a list of 10-18 generic keywords used as a
coarse precision gate in `filters/stop_protocol.py`. The prompt
explicitly tells the LLM to **include the single-word root** of the
topic alongside any phrases:

```
Topic "strategic partnerships" -> [
  "partnership", "partnered", "alliance", "joint venture",
  "collaboration", "agreement", "deal", ...
]
```

Why this matters: an article titled "DoorDash expands in Canada with
Empire Partnership" must match somewhere. If the keywords are all
multi-word phrases like `"strategic partnership"`, the title fails
because "Empire" is between the two required words. The single-word
`partnership` root catches it.

The keyword matcher in `_matches_keywords()` does prefix matching for
single tokens (`"layoff"` matches `"layoffs"`, `"layoff"`, `"laid"`)
which combined with the single-word root strategy gives high recall
on the coarse gate.

## References

- SearXNG search syntax: https://docs.searxng.org/user/search-syntax.html
- Internal: `tools/queries.py:_QUERY_PROMPT` for the active prompt
- Internal: `tools/topic.py:_KEYWORD_PROMPT` for keyword generation
- Internal: `filters/stop_protocol.py:_matches_keywords` for the gate
- Internal: `filters/ranker.py` for the credibility-domain bonus
