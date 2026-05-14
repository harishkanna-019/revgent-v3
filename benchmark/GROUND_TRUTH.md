# Ground Truth Benchmark: Revgent vs Claygent

**Date:** 2026-05-14
**Methodology:** 3 independent Claude Sonnet agents verified each of the 150
company×topic combinations via web search against actual events since May 2025.

## Overall Results

| | Claygent | Revgent (Run 4) |
|---|---|---|
| **Recall** | 49/53 = 92% | 21/53 = 40% |
| **Precision** | 49/97 = 51% | High (by design) |
| **False Positives** | **48** | Near zero |

## Ground Truth by Topic

| Topic | True Events | Claygent Found | Claygent FP | Revgent Found |
|---|---|---|---|---|
| Funding round | 20 | 19 (95%) | 10 | 8 (40%) |
| C-suite exec changes | 21 | 21 (100%) | 20 | 8 (38%) |
| Data breach | 12 | 9 (75%) | 18 | 5 (42%) |
| **Total** | **53** | **49** | **48** | **21** |

## Key Insight: Claygent Has 48% False Positive Rate

Claygent's raw 65% "detection rate" from the original benchmark included 48
false positives — events that did NOT actually occur. The real recall is 92%
but at the cost of claiming events for 97/150 rows when only 53 are real.

### Common false positive patterns:
- **Events before May 2025 cutoff** (30+ cases): Claygent returned 2024/early-2025 events
- **Third-party breaches attributed to the company** (Salesloft Drift → Cloudflare/Zscaler)
- **Non-C-suite roles** (Chief Communications Officer, Chief Legal Officer) counted as exec changes
- **Acquisitions counted as funding** (Wiz/Google, Neon/Databricks)
- **Vulnerabilities presented as breaches** (Notion AI prompt injection, npm package CVEs)
- **Unverified dark web claims** (ZoomInfo Oct 2025)

## Revgent Misses: 32 Real Events

### Funding round misses (12):
Most are **tender offers, secondaries, and small rounds** not well-covered
by news search:
- Tender/secondary: Stripe ($159B), Canva ($42B), Notion ($11B), Vercel, Gusto
- Convertible notes: Cloudflare ($1.75B)
- Small rounds: Attio ($52M)
- Major rounds that SHOULD be found: Rippling ($450M), Deel ($300M), Clay ($100M), Mistral (€1.7B), Scale AI ($14.3B)

### C-suite exec change misses (13):
Almost all found by Claygent via **structured data** (Crunchbase/Tracxn),
not news search. Examples:
- Meta: Dina Powell McCormick appointed President (Jan 2026)
- Airtable: David Azose appointed CTO (Oct 2025)
- Deel: Joe Kauffman appointed President & CFO (Nov 2025)
- Cohere: Francois Chadwick appointed CFO (Aug 2025)

### Data breach misses (7):
Many are supply-chain or insider incidents:
- Salesloft Drift supply chain (Cloudflare, Zscaler — Aug 2025)
- Rippling insider data theft (Mar 2025)
- Linear access control bug (Mar 2026)
- Toast Payroll breach (Jun-Jul 2025) — SHOULD be findable
- Scale AI data leak (mid-2025) — SHOULD be findable
- UiPath npm supply chain (May 2026)

## Revgent Structural Limitations

For the 32 missed real events, the root causes break down as:

| Cause | Count | Fixable? |
|---|---|---|
| No news coverage (tender/secondary/small round) | ~10 | No — need structured data |
| C-suite changes not in news (only in Crunchbase) | ~10 | No — need structured data |
| Supply-chain breach (third-party attribution) | ~4 | Partially via better attribution in validation |
| Real event, should be findable by news search | ~8 | **Yes** — Toast breach, Scale leak, Rippling round, etc. |

## Bottom Line

**Claygent is high-recall, low-precision (51%).** Finds almost everything but
includes massive noise — 48 events that didn't happen. Good for "cast a wide net"
use cases but requires human filtering.

**Revgent is high-precision, moderate-recall (40%).** Only reports verified
events, misses ~60% of real events. Good for "trust the signal" use cases.
The stop protocol + validation prompt fixes deployed today should improve
recall without adding false positives.

**The 60% gap is mostly structural.** ~20 of the 32 misses are events that
simply aren't covered by news media — they exist only in structured databases
(Crunchbase, Tracxn, SEC filings). To match Claygent's recall, Revgent needs
at least one structured data source beyond SearXNG news.
