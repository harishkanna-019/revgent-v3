# LinkedIn People Search via SearXNG

Investigation log — May 2026. Goal: find named individuals working at
a target company by title pattern, using only search snippets (never
scraping LinkedIn directly, which would get blocked).

## TL;DR

**It's possible but fragile.** When upstream search engines cooperate,
SearXNG can return 10 LinkedIn profile URLs in a single query with
enough snippet detail (name, title, employer, location, tenure) to
build a structured prospect record **without ever loading
linkedin.com**.

The blocker is **engine reliability**. Of the 8 engines we tested,
**only Google indexes LinkedIn profile pages deeply**, and Google's
bot detection blocks our SearXNG instance within ~10 queries.

## What actually worked

In a clean session, this query returned 10 LinkedIn profiles
immediately:

```
site:linkedin.com/in "doordash" "head of partnerships"
```

Sample result:
```
Olivia (Ulam) Williams - Head of Partnerships Marketing at DoorDash
https://www.linkedin.com/in/olivia-ulam-williams-43002154

Snippet: "VP @ DoorDash - Experience: DoorDash - Education: Brown
University - Location: San Francisco Bay Area - 500+ connections on
LinkedIn."
```

Snippet contains everything we need:
- Full name (from URL slug + title)
- Current title and employer (from page title)
- Location (from snippet body)
- Tenure (from snippet body, when present)

No LinkedIn scrape required.

## Query patterns that get LinkedIn profile hits

| Pattern | LinkedIn profiles | Notes |
|---|---|---|
| `site:linkedin.com/in "doordash" "head of partnerships"` | 10/10 | Best baseline |
| `site:linkedin.com/in "doordash" "vp partnerships"` | 5/10 | Mixed - includes some ex-employees |
| `site:linkedin.com/in "doordash" "director of partnerships"` | 3/10 | Other companies leak in |
| `site:linkedin.com/in "doordash" "business development"` | 6/10 | Solid |
| `site:linkedin.com/in "at doordash" "partnerships"` | 10/10 | "at" qualifier reduces ex-employee noise |
| `"linkedin.com/in" "doordash" "head of partnerships"` | 10/10 | Drop `site:`, treat URL as substring |
| `inurl:linkedin.com/in "doordash" "head of partnerships"` | 2/10 | Worse than `site:` |

The clear winners:
- `site:linkedin.com/in` + `"company"` + `"title"` — strict, high precision
- `"linkedin.com/in"` + `"company"` + `"title"` — looser substring, similar recall

## The engine-reliability problem

When we ran the same baseline query 4 times in succession, we got:

| Run | Total results | LinkedIn profiles | Active engines |
|---|---|---|---|
| 1 | 10 | 10 | google |
| 2 | 0 | 0 | (all blocked) |
| 3 | 0 | 0 | (all blocked) |
| 4 | 0 | 0 | (all blocked) |

`unresponsive_engines` reported:

```
brave:       Suspended: too many requests
duckduckgo:  CAPTCHA
karmasearch: Suspended: access denied
startpage:   Suspended: CAPTCHA
```

**Only Bing was still answering**, and Bing doesn't honor `site:linkedin.com/in`
properly — it returns LinkedIn login pages instead of profile URLs.

This means: our **production SearXNG can answer 1-3 LinkedIn queries
per minute reliably**, then degrades to zero results until rate-limit
windows reset (typically 1-2 hours for Brave/Startpage).

## Which engines actually index LinkedIn profiles

Tested via `!bang` operator:

| Engine | LinkedIn profile coverage | Reliability against our SearXNG |
|---|---|---|
| `!google` | Best — full profile pages indexed | Blocked after ~10 queries |
| `!duckduckgo` | Good — uses Bing index, but enriches with own crawler | CAPTCHA blocks production |
| `!bing` | Returns linkedin.com URLs but only homepage / login / company pages, not profile deep links | Reliable, low precision |
| `!brave` | Indexes profiles | Rate-limited heavily |
| `!startpage` | Proxies Google, same coverage | CAPTCHA blocks production |
| `!qwant` | Limited LinkedIn coverage | Mostly works, sparse results |
| `!yep` | Marginal | Mostly works |
| `!mwmbl` | None (community-curated, not commercial) | Always answers, zero LinkedIn |

## What you can extract from snippets alone

For each LinkedIn profile result, the snippet typically contains:

```
Field            Source                          Reliability
-----            ------                          -----------
Full name        page title + URL slug           High
Current title    page title before " - "         High
Current employer page title after " at "         High
Location         snippet body "Location: ..."    Medium (~70%)
Tenure           snippet body "X years Y months" Medium (~50%)
Past employers   snippet body "Previously..."    Medium (~40%)
Education        snippet body "Education: ..."   Low (~30%)
Connection count snippet body "500+ connections" Mostly always present
```

**This is enough to populate a Clay prospect row** with:
- name
- title
- company
- location
- LinkedIn URL (for Clay's own LinkedIn enrichment to fill the rest)

We never need to scrape LinkedIn. The snippet is the payload.

## Snippet parsing example

Raw snippet:
```
VP, Finance and Investor Relations. DoorDash. Aug 2020 - Present
5 years 10 months ; Research Analyst/Consultant. Sands Capital
Management. May 2020 ...
```

Parsed fields:
```python
{
  "name": "Andy Hargreaves",                   # from title
  "current_title": "VP, Finance and Investor Relations",
  "current_company": "DoorDash",
  "tenure_at_current": "Aug 2020 - Present (5y 10mo)",
  "previous_role": "Research Analyst/Consultant",
  "previous_company": "Sands Capital Management",
  "linkedin_url": "https://www.linkedin.com/in/andy-hargreaves-4833354",
}
```

A deterministic regex over 200-char snippets gives ~80% field-fill
rate without any LLM.

## Recommendations for the agent

### Query strategy

1. **Use `site:linkedin.com/in`** as the primary operator. Drop to
   `"linkedin.com/in"` substring form if `site:` returns nothing
   (signals Google is throttled and only Bing is answering).
2. **Always quote the company name** if multi-word. Same precision lift
   we documented in `SEARCH-SYNTAX.md`.
3. **Use the `"at {company}"` qualifier** to filter out ex-employees:
   `site:linkedin.com/in "at doordash" "partnerships"`.
4. **Spread queries across title variants** in parallel (head /
   director / VP / SVP / chief of {function}). One query per variant.
5. **Cap retries at 1**. If the second attempt returns 0, the engine
   is rate-limited; switch to a different title variant rather than
   re-querying the same string.

### Backoff and fallback

The agent should treat zero LinkedIn results not as "this title doesn't
exist at this company" but as "the engine is currently throttled". To
distinguish:

- If query A returns 0 LinkedIn profiles AND query A+10s also returns 0:
  treat as **engine throttled**, fall back to other engines or wait
- If query A returns 10 LinkedIn profiles for the company but query B
  with different title returns 0: treat as **no such title** at the
  company

### Snippet-only mode

For production reliability, the pipeline should **never try to fetch
linkedin.com directly**. LinkedIn aggressively blocks scrapers and
returns "Sign in to view" pages even on apparently public profiles.
The SearXNG snippet is the deliverable.

## What this would look like in the existing pipeline

A new tool `tools/people.py` mirroring our news pattern:

```python
async def find_people(
    ctx: RunContext,
    company: str,
    title_patterns: list[str],
) -> list[dict]:
    """Find LinkedIn profiles matching title_patterns at company.

    Returns list of {name, title, company, location, linkedin_url, ...}
    parsed from search snippets only. Never fetches linkedin.com.
    """
```

Stages:
1. **Query generation** — for each title pattern, build:
   `site:linkedin.com/in "{company}" "{title_pattern}"`
   plus the "at company" variant.
2. **Parallel search** — fan out to SearXNG with `parallel()`.
3. **URL filter** — keep only `linkedin.com/in/...` URLs, drop login
   pages, company pages, posts.
4. **Snippet parser** — deterministic regex extracting name, title,
   employer, location.
5. **Current-employee filter** — drop entries where the title or
   snippet contains "ex-", "former", "previously at" referencing the
   target company.
6. **Dedup by profile slug** — `linkedin.com/in/{slug}` is unique.

Estimated cost per query: ~$0 (no LLM calls needed if regex parsing
is enough). Optionally add a `format()` LLM step for high-quality
fields, would push cost to ~$0.0001 per profile.

## Why this matters

A working LinkedIn person-search is the **second-most-asked Clay
column** after news enrichment. Today users pay $0.04+ per profile to
proxy through commercial scrapers (Apollo, Lusha, ContactOut).
A SearXNG-snippet pipeline could deliver the same record at $0 with
~80% of the field coverage — good enough for tier-1 prospect
research.

The blocker isn't the syntax or our pipeline. It's getting a
SearXNG that can keep Google + DuckDuckGo + Brave answering
consistently. Options:

1. **Self-host SearXNG with residential proxies** — the standard
   way commercial scrapers solve this. Costs ~$30-100/mo for
   residential IPs but eliminates the throttle.
2. **Add a paid SerpAPI / ScaleSerp fallback** — ~$0.001 per
   query, only used when SearXNG returns 0. Hybrid mode.
3. **Multi-region SearXNG cluster** — three SearXNG instances in
   different regions, round-robin. Triples the rate budget.
4. **Accept the limitation** — only use this for low-volume,
   on-demand person-search, not batch enrichment.

## Conclusion

LinkedIn people-search **works in principle** through our existing
SearXNG infrastructure with no changes to the pipeline architecture.
It would require:

- ~1 day to add `tools/people.py` and snippet-parsing regex
- Resolution of the upstream-engine throttle problem (residential
  proxies on a self-hosted SearXNG is the cleanest fix)

Without the throttle fix, the feature works for **demos and one-off
lookups** but not for batch Clay enrichment.
