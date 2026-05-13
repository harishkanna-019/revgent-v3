# Meeting notes — Revgent v3 review, 13 May 2026

Attendees: Harish Kanna, Dominykas Rukas, Jack Cane, Britney Petrova.

## Decisions confirmed

| Topic | Decision |
|---|---|
| Architecture direction | **Open-source stack stays**: Railway host + OpenRouter LLM + SearXNG search. No move to Tavily or paid search. |
| Cost ceiling | ~$20 / mo Railway + ~$20 / mo OpenRouter = ~$40 / mo for a 1,500-account batch ran 2x / week. Cheaper than Clay agent. |
| Search engine | SearXNG aggregating Bing + DuckDuckGo + Google + others. **Open question** — Dominykas to evaluate Google Search API direct (free tier may suffice for narrow client use). |
| LinkedIn search | Possible through SearXNG but needs **residential proxies** (~$80 / mo extra) to bypass Google's bot detection. Documented in `docs/LINKEDIN-SEARCH.md`. Deferred. |
| Query generation | Stays LLM-driven for now, but **must log generated queries** so we can audit drift run-over-run. Implemented in this commit. |
| Search depth | Cheap = 3-4 sources, standard = 5-7, deep = 12-15. Keep, but accept "depth equals truth" — see follow-up below. |

## The big pivot — running signal engine, not one-shot lookup

Jack pushed back on the existing assumption that Revgent is a "research
on demand" tool. The team aligned on a different model:

> The first run captures **state**. Every subsequent run compares
> against that state and only surfaces what's **new since last run**.

This changes the architecture meaningfully:

| Aspect | Today (one-shot) | Where we're going (state-comparison) |
|---|---|---|
| Trigger | Clay column calls / user request | Scheduled (every 3 days) per account |
| Date window | "last 30/90 days from today" | "since last successful run" |
| Output on no change | Same N events as before (re-validated) | **Empty** — no signal to push |
| Output on change | Same as before | New events only, framed as "X happened since last check" |
| Stop condition | Budget / found enough events | **No new articles since last run** = skip everything else |
| Cost on re-run | Full pipeline cost every time | ~$0 on no-change re-runs (cache hit only) |

### Why this matters

For SciSure (the clearest example given in the meeting): initial
campaign uses one-shot mode to find companies that recently had a
signal. Once outreach is going, the same accounts get monitored on a
3-day cadence and we only send to HubSpot/Clay when something genuinely
new is detected.

### Connection to cross-run cache (issue #32)

Issue #32 currently scopes cross-run dedup as a **cost optimization**.
This meeting reframed it as the **foundation for state-comparison
mode**. The cache stops being optional and becomes load-bearing.

Action: update issue #32 to reflect the broader scope, or split it
into two issues (cache infrastructure + state-comparison logic). See
"Issues to file" below.

## Action items committed in the meeting

| @timestamp | Owner | Item | Status |
|---|---|---|---|
| 46:40 | Harish | Add debug log of generated queries to `/research/clay` response | **DONE in this commit** |
| 25:48 | Dominykas | Evaluate Google Search API costs + rate limits as alternative to SearXNG aggregator | Pending |
| 30:18 | Jack | Reframe Revgent as state-comparison engine (write up architecture) | This doc + issue to file |
| 31:14 | Jack | Investigate embedding-based article grouping before LLM evaluation | Issue to file |
| 37:48 | Dominykas | Sliding date window per account (last successful run, not absolute days) | Issue to file |
| 50:23 | Britney | After exam: research efficient state-lookup / caching patterns for the new architecture | Pending |
| 47:15 | Jack | Per-client config: each client gets their own repo copy with hand-tuned topic→query mapping | Issue to file |

## Concrete change in this commit

Added `queries_used`, `topic_simplified`, `topic_keywords` to:

- `core/context.py:RunContext.build_response()` — new `debug` block
- `api.py:/research/clay` — surfaced as top-level fields and logged

Sample `/research/clay` response now includes:

```json
{
  "company": "meta.com",
  "event_count": 5,
  ...
  "queries_used": [
    "\"meta\" layoffs",
    "\"meta\" layoffs news 2026",
    "\"meta\" (layoffs OR \"job cuts\" OR fired)",
    "\"meta\" layoffs 2026"
  ],
  "topic_simplified": "layoffs",
  "topic_keywords": ["layoff", "workforce", "fired", "termination"]
}
```

The production log line now ends with `queries=[...]` so we can grep
Railway logs and audit query drift without replaying requests.

### Why this matters per the meeting

Jack's concern: LLM-generated queries change run-over-run, so a "no
new signal" result might be lies — we may have just generated different
queries and missed the same article we found last time. Surfacing the
queries lets us:

1. Compare run-over-run query strings for the same `(company, topic)`
   pair and verify the LLM is stable.
2. Hand-write a reference query set in the per-client repo and compare
   LLM output against it as a quality gate.
3. Build the eventual per-client config (item @47:15) by **harvesting
   the best queries we've seen for each topic** instead of starting
   from scratch.

## Issues to file (separate from this commit)

| # | Title | Scope | Effort |
|---|---|---|---|
| TBD | Reframe Revgent as state-comparison engine | Architecture doc + spike on the diff logic + per-account "last run state" persistence | 3-5 days |
| TBD | Sliding date window from last successful run | Replace `date_max=90` semantics with `since_last_run_or_X_days` | 1 day |
| TBD | Per-client topic→query template registry | Each client has a YAML mapping `topic -> [hand-written queries]` that overrides LLM generation when present | 1-2 days |
| TBD | Embedding-based article grouping | Group N articles into K clusters before LLM evaluation. Use OpenRouter or local sentence-transformers. | 1-2 days (spike) |
| #32 (update) | Cross-run URL cache — promote scope | Currently a cost optimization; rewrite as foundation for state-comparison | n/a (scope-only change) |

## Open questions

- **Google Search API direct** — what's the actual cost vs SearXNG aggregator at our query volume? (Dom investigating)
- **Embedding cost vs evaluation cost** — embeddings are 2-3 orders of magnitude cheaper than LLM evaluation per item, but only useful if we have enough articles per request to make grouping worthwhile. Jack's intuition: yes; my intuition: maybe only on broad topics. Needs measurement.
- **Per-client repo proliferation** — if each client has their own Revgent fork, do we deploy N Railway services, or one shared service that loads per-client config? Jack flagged this could become unmanageable.

## Not changing today

- Architecture stays as-is for the next sprint. State-comparison is a
  significant pivot and needs its own design doc before implementation.
- LinkedIn person search stays as the documented investigation in
  `docs/LINKEDIN-SEARCH.md`. Not productionizing until proxy/throttle
  problem has a real solution.
