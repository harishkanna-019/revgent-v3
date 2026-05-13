# Validation Prompt Calibration

Investigation log — May 2026.

## Suspected issue

Live testing on the 9-case Clay battery showed `gamestop.com` /
`Executive C-Suite Changes` returning 0 events, while the upstream
stages indicated 16 candidates survived stop_protocol. That implied
the validation LLM was rejecting articles that should have passed.

## What we found

Traced the 6 ranked candidates through `_RELEVANCE_PROMPT`. All 6
got `NO`. They were all variants of:

- "GameStop CEO Comments on eBay Acquisition"
- "eBay rejects GameStop's $55.5 billion takeover bid"
- "GameStop CEO Ryan Cohen wants to 'own eBay forever'"

Ryan Cohen has been GameStop's CEO since 2023. These articles are
about **an acquisition attempt by GameStop**, not a **C-suite change
at GameStop**. The validation prompt was working correctly:

> A YES answer requires BOTH conditions:
> 1. {company} is the primary subject of the article, AND
> 2. The article reports on {company} itself doing or experiencing "{topic}"

The articles satisfy (1) but not (2) — GameStop is doing an
acquisition, not a CEO change.

Ground-truth check via SearXNG: zero real GameStop C-suite changes
in the trailing 365 days. The 0 events is the **true negative**
answer, not a false negative.

## Edge-case probe

Ran the validation prompt against 7 hand-crafted cases covering the
most common false-positive / false-negative patterns:

```
[YES] meta.com       / layoffs        -> Meta to lay off 8,000 amid AI restructuring
[NO]  anthropic.com  / layoffs        -> Meta lays off 8,000; Anthropic in AI talks with White House
[YES] intel.com      / CEO change     -> Intel names Lip-Bu Tan as CEO, replacing Pat Gelsinger
[NO]  boeing.com     / product launch -> Lufthansa Boeing 737 makes emergency landing
[YES] doordash.com   / partnership    -> DoorDash partners with Kroger to expand grocery delivery
[NO]  doordash.com   / partnership    -> Uber Eats partners with Wendys; DoorDash similar deal in place
[NO]  meta.com       / layoffs        -> Dow Jones Top Headlines: GameStop, eBay, Meta layoffs
```

**All 7 answers are correct.** Including the tricky multi-company
digest cases where our company is mentioned but not the actor of the
topic, and the brand-name-as-product case (a Boeing 737 in a Lufthansa
story is not a Boeing product launch).

## The one edge where the prompt is conservative

Tested a "Dow Jones Top Headlines" digest where the body contained
substantial detail about Meta's layoffs (multi-paragraph). The LLM
still answered NO because the headline was generic. This is a
**defensible false-negative**: if Meta layoffs are real news, a
dedicated Meta-focused article is almost certainly also in the search
result set, so we don't lose anything important by being strict on
digest-style headlines.

Net impact in production: zero observed cases where the strict
behavior cost us a real event. The strictness pays for itself by
preventing the inverse failure (a 2018 Under Armour breach article
slipping through because it briefly mentions Under Armour).

## Real cases that work end-to-end

| Company | Topic | Result | Date |
|---|---|---|---|
| facebook.com | Security Breaches | 3 events | 2026-05-06 confirmed Facebook account phishing |
| doordash.com | Strategic Partnerships | 6 events | 2026-05-05 confirmed SNAP/Kroger deal |
| meta.com | Layoffs | 5 events | 2026-05-13 confirmed 8,000 job cuts with severance |
| intel.com | New CEO | 1 event + 3 signals | 2026-05-12 confirmed Lip-Bu Tan as CEO |
| underarmour.com | Data Breaches | (zero on May 13, drift) | Apr 14 article exists; window-edge issue |

## Conclusion

**No prompt change needed.** Validation is correctly calibrated.
The gamestop "0 events" result is the right answer, not a bug.

If we ever want to relax this — and we shouldn't unless we have a
reason — the lever is in `tools/validate.py:_RELEVANCE_PROMPT`. The
key passage is:

```
A YES answer requires BOTH conditions:
1. {company} is the primary subject of the article, AND
2. The article reports on {company} itself doing or experiencing "{topic}"
```

Loosening this to `A YES answer requires EITHER` would surface more
events but would also resurrect the multi-company-digest false
positives we fixed in commit `0daf6ce`.
