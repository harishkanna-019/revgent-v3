# Revgent vs Claygent Benchmark Results

## Setup
- 50 companies x 3 signals = 150 queries
- Signals: Funding/M&A, C-suite Changes, Data Breach
- Companies: B2B SaaS/tech, mixed tiers (enterprise/growth/mid-market)
- Claygent: best-effort structured prompt, Google Search, 12-month window
- Revgent: standard depth, SearXNG, date_max=365

## Scorecard

| Metric | Claygent | Revgent |
|---|---|---|
| Detection rate | **65%** (97/150) | 16% (24/150) |
| Detection (events+signals) | -- | 20% (30/150) |
| Avg cost/query | $0.0034 | **$0.00038** (9x cheaper) |
| Avg latency | 21.4s | **12.1s** |
| Total cost (150 queries) | $0.51 | **$0.057** |
| Avg tokens/query | 10,187 | **4,876** |

## Why Revgent Detects Less

### 73 misses broken down:

| Failure Mode | Count | % | Status |
|---|---|---|---|
| Validation rejected all scraped articles | 41 | 56% | FIXED (commit ffce0ab) |
| Stop protocol killed all candidates | 24 | 33% | Open |
| Format/route dropped validated articles | 6 | 8% | Open |
| No search results | 2 | 3% | Open |

### Fix deployed: company name in validation prompt

The validation LLM prompt was asking "Is this article about **meta.com**?"
instead of "Is this article about **meta platforms**?". News articles never
reference companies by domain. The LLM correctly answered NO.

- Commit: `ffce0ab`
- Impact: 41/150 queries (27%) had this as sole failure cause
- Confirmed live: `meta.com + data breach` flipped from 0 to 1 event;
  `snowflake.com + C-suite` flipped from 0 to 1 event
- Not all 41 will flip: some scraped articles are genuinely off-topic

### Remaining issues

1. **Stop protocol too aggressive (24 misses)** — SearXNG snippets are too
   short to contain both company name AND topic keyword. Articles get killed
   before scraping. Fix: loosen keyword matching or skip company-name check
   in stop protocol (the LLM validation handles it downstream).

2. **Format/route drops (6 misses)** — Articles pass validation but the
   fact-check LLM classifies them as "opinion" instead of "hard fact".
   They become signals, not events. Examples: "Meta raises $25B in debt"
   classified as "analyst commentary".

3. **Search coverage gap** — Claygent uses Google Search + Clay dossier data
   (Crunchbase, Tracxn). For funding rounds, Claygent visits structured
   databases. Revgent only has SearXNG news search.

## What Revgent Does Better

- **9x cheaper** per query ($0.00038 vs $0.0034)
- **1.8x faster** (12.1s vs 21.4s)
- **Structured pipeline trace** for debugging (every stage logged)
- **Query audit trail** (exact queries visible in response)
- **Source URLs always provided** (Claygent sometimes omits)
- **Date precision** (YYYY-MM-DD from article metadata, not LLM inference)

## True Negatives (both agree: no events)

53/150 rows where both tools found nothing. Breakdown:
- Funding round: 21 companies (correct — most are public/post-IPO)
- Data breach: 23 companies (correct — no known breaches)
- C-suite changes: 9 companies (correct — stable leadership)
