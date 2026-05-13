# Revgent vs Claygent Benchmark Results

## Setup
- 50 companies x 3 signals = 150 queries
- Signals: Funding/M&A, C-suite Changes, Data Breach
- Companies: B2B SaaS/tech, mixed tiers (enterprise/growth/mid-market)
- Claygent: best-effort structured prompt, Google Search, 12-month window
- Revgent: standard depth, SearXNG, date_max=365

## Three Runs

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| **Config** | date_max=90, old code | date_max=365, sequential | date_max=365, 150 concurrent |
| **Code fix** | no | no | yes (company name) |
| **Detection** | 2% | 16% | 3% |
| **Timeouts** | 29% | 0% | 33% |
| **Avg latency** | 63s | 12s | 62s |
| **Avg cost** | $0.00031 | $0.00038 | $0.00030 |

Run 2 is the true apples-to-apples comparison (sequential, no timeouts).
Run 3 regressed because Clay fired 150 requests simultaneously at a single
Railway instance, causing 33% timeouts.

## Scorecard (Run 2 — fair comparison)

| Metric | Claygent | Revgent |
|---|---|---|
| **Detection rate** | **65%** (97/150) | 16% (24/150) |
| Detection (events+signals) | -- | 20% (30/150) |
| Avg cost/query | $0.0034 | **$0.00038** (9x cheaper) |
| Avg latency | 21.4s | **12.1s** (1.8x faster) |
| Total cost (150 queries) | $0.51 | **$0.057** |

## Root Causes (73 misses in Run 2)

| Failure Mode | Count | % | Fix |
|---|---|---|---|
| Validation rejected scraped articles | 41 | 56% | FIXED (ffce0ab) — prompt said "meta.com" not "meta platforms" |
| Stop protocol killed all candidates | 24 | 33% | Open — SearXNG snippets too short for keyword match |
| Format/route dropped as "opinion" | 6 | 8% | Open — fact-check LLM too conservative |
| No search results at all | 2 | 3% | Open — SearXNG coverage gap |

## Root Causes (Run 3 — concurrent load)

| Failure Mode | Count | % |
|---|---|---|
| Both agree: no events | 52 | 35% |
| **Timeout (90s)** | **36** | **24%** |
| Validation rejected | 29 | 19% |
| Stop protocol killed | 25 | 17% |
| Format/route dropped | 4 | 3% |
| Both found | 3 | 2% |
| Revgent only | 1 | 1% |

The concurrency issue is the dominant factor in Run 3. 150 simultaneous
requests overwhelm the single Railway instance (512MB / 1 CPU). Semaphores
(LLM=24, search=12, scrape=8) throttle external calls but internal queue
latency eats the 90s timeout budget.

## Fixes Needed (priority order)

### 1. Concurrency / scaling (blocks everything)
- Railway instance can handle ~10-20 concurrent standard-depth requests
- Clay fires all rows simultaneously (up to 150)
- Options: scale Railway to multiple replicas, or use `/research/async`
  with webhook so Clay doesn't wait per-row

### 2. Validation company name (DONE)
- Commit `ffce0ab` — uses resolved name like "meta platforms" instead of "meta.com"
- Confirmed working on single requests

### 3. Stop protocol loosening
- 24 misses from stop protocol killing candidates with 40-70 search results
- SearXNG snippets too short to match both company + topic keyword
- Fix: relax company-name check in stop protocol (LLM validation downstream
  handles company relevance more accurately)

### 4. Fact-check calibration
- 6 misses from articles classified as "opinion" that are actually facts
- Example: "Meta raises $25B in debt" classified as "analyst commentary"

## What Revgent Does Better

- **9x cheaper** per query ($0.00038 vs $0.0034)
- **1.8x faster** at low concurrency (12.1s vs 21.4s)
- **Structured pipeline trace** for debugging
- **Query audit trail** in response
- **Source URLs always provided**
- **Deterministic date extraction** from article metadata

## Structural Gap

Claygent has Google Search + Clay dossier (Crunchbase, Tracxn structured data).
For "funding round" queries, Claygent visits clay.com/dossier/{company}-funding
which has complete funding history. Revgent only has SearXNG news search,
so it can only find funding events covered in news articles.
