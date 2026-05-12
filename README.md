# revgent

Async corporate news signal engine. Takes `(company, topic, depth)` and returns structured events, signals, and a confirmed primary source — designed for Clay batch enrichment.

```
POST /research/clay
{"company": "coinbase.com", "topics": ["layoff"], "depth": "standard"}

→ {
    "primary_headline": "Coinbase to cut about 14% of workforce in AI-driven restructuring",
    "primary_date":     "2026-05-07",
    "primary_source_name": "msn.com",
    "summary":          "Coinbase announced it will cut approximately 700 jobs...",
    "is_valid":         true,
    "confidence":       "high",
    "event_count":      5,
    "signal_count":     3,
    "total_cost_usd":   0.00122,
    "elapsed_ms":       56084,
    "events":   [...],
    "signals":  [...],
    "answers":  [...]
  }
```

**One Clay row = ~$0.0006 in API fees, ~30 s wall time. 1,000 companies = ~$0.63.**

---

## Why it exists

Clay can already enrich a company with firmographics. What Clay can't do is research a specific topic about a company (recent layoffs, funding round, leadership change, product launch) and return a structured, source-cited event.

revgent fills that gap. It's the "signal engine" half of the Revenanas service offering (MOD-02 Signal-Led ABM, MOD-08 Proactive Trigger System), exposed as an HTTP API that Clay calls as a regular enrichment column.

---

## Architecture at a glance

```
api.py (FastAPI)
  └── core/pipeline.py            11-stage orchestration
        ├── tools/topic.py        topic keyword expansion (LLM)
        ├── tools/queries.py      query generation (pure)
        ├── providers/search.py   SearXNG fan-out
        ├── filters/dedup.py      URL + content-hash dedup (pure)
        ├── filters/stop_protocol.py  date + domain + keyword gates (pure)
        ├── filters/ranker.py     authority + recency scoring (pure)
        ├── providers/scrape.py   trafilatura + SSRF guard
        ├── tools/validate.py     "is X the actor of topic?" (LLM)
        ├── tools/format.py       classify + extract date + summarize (LLM, parallel)
        ├── filters/signals.py    soft-signal routing (pure)
        └── answer_builder.py     sort + pack response (pure)
```

Four layers, top-down imports only:

```
api      → core
core     → tools, filters, providers (via tools)
tools    → providers
filters  → nothing external
providers → external services only
```

Every I/O call is `async`. Every external call goes through exactly one semaphore (LLM 24, search 12, scrape 8). One failed candidate in a parallel stage never cancels the others.

See `docs/ARCHITECTURE.md`, `docs/MODULES.md`, and `docs/RULES.md` for the full design.

---

## Three depth modes

| Setting | cheap | standard | deep |
|---|---|---|---|
| Use case | "Has anything happened?" pre-qualification | Full Clay enrichment | High-stakes accounts |
| Avg cost / request | ~$0.00007 | ~$0.00063 | ~$0.0025 |
| Avg latency | ~10 s | ~30 s | ~60 s |
| Max workers | 4 | 8 | 12 |
| Timeout | 45 s | 120 s | 240 s |
| Topic + validate + format model | flash | flash | pro |
| Synthesis model | flash | pro | kimi-k2.6 |

Cost projection for 1,000 Clay rows:

| Depth | Cost | Wall time @ 10 concurrent | Wall time @ 50 concurrent |
|---|---:|---:|---:|
| cheap | **$0.07** | ~17 min | ~3.5 min |
| standard | **$0.63** | ~63 min | ~13 min |
| deep | ~$2.50 | ~100 min | ~20 min |

---

## Quick start

### Local

```bash
pip install -r requirements.txt
cp .env.example .env             # add OPENROUTER_API_KEY
uvicorn api:app --reload         # binds :8000
curl http://localhost:8000/      # health check
```

### CLI

```bash
python -m revgent.cli --company coinbase.com --topics layoff --depth standard
```

### Docker

```bash
docker build -t revgent .
docker run -d --name revgent \
  -e OPENROUTER_API_KEY="..." \
  -e SEARXNG_URL="http://host.docker.internal:8888" \
  -e REVGENT_API_KEY="$(openssl rand -hex 16)" \
  -p 8000:8000 --memory=512m --cpus=1.0 \
  revgent
```

### Railway

```bash
railway login
railway init
railway up                       # uses Dockerfile + railway.toml
```

Then add a SearXNG service from Railway's one-click template, set `OPENROUTER_API_KEY` / `SEARXNG_URL` / `REVGENT_API_KEY` on the revgent service, and you're live. **Hobby plan ($5/mo) is enough for up to 10k Clay rows/day** — the service is I/O-bound and runs on ~150 MB RAM and <5% CPU sustained.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Health check |
| POST | `/research` | Full v3 response (nested) |
| POST | `/research/clay` | Flat scalars for Clay column mapping |
| POST | `/research/async` | Returns `request_id`, processes in background |

All `/research*` endpoints honor an optional `X-Api-Key` header if `REVGENT_API_KEY` is set.

### Request body

```json
{
  "company": "coinbase.com",
  "topics":  ["layoff", "funding"],
  "depth":   "standard"
}
```

Backward-compat: `company_domain` is accepted as an alias for `company`.

### Response — `/research/clay` (Clay-friendly flat fields)

| Field | Type | Meaning |
|---|---|---|
| `primary_headline` | str | Top-ranked event's headline |
| `primary_source_url` | str | Canonical confirmation URL |
| `primary_source_name` | str | Hostname of `primary_source_url` |
| `primary_date` | str | ISO date or `"Unknown"` |
| `summary` | str | LLM-written summary of the top event |
| `is_valid` | bool | `true` if at least one hard-fact event found |
| `confidence` | str | `"high"` / `"medium"` / `"low"` |
| `event_count` | int | Number of hard-fact events |
| `signal_count` | int | Number of soft signals |
| `signal_type` | str | First soft-signal type if any |
| `signal_confidence` | float | First soft-signal confidence |
| `total_cost_usd` | float | OpenRouter spend for this request |
| `total_tokens` | int | Total tokens consumed |
| `elapsed_ms` | int | End-to-end wall time |
| `request_id` | str | 8-char hex for log correlation |
| `events` | list | Full event objects |
| `signals` | list | Soft-signal objects |
| `answers` | list | Validated answer objects with sources |
| `stage_trace` | list | Per-stage `out_count` for debugging |

See `docs/CLAY.md` for the full Clay column-mapping recipe.

---

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | OpenRouter authentication |
| `SEARXNG_URL` | recommended | `http://localhost:8888` | Self-hosted SearXNG endpoint |
| `REVGENT_API_KEY` | no | — | If set, `/research*` requires `X-Api-Key` header |
| `LLM_CONCURRENCY` | no | 24 | Max concurrent OpenRouter calls |
| `SEARCH_CONCURRENCY` | no | 12 | Max concurrent SearXNG queries |
| `SCRAPE_CONCURRENCY` | no | 8 | Max concurrent page scrapes |
| `DEFAULT_MAX_COST` | no | per-depth | Override budget ceiling |
| `ABSOLUTE_MAX_COST` | no | — | Hard ceiling on `max_cost` in any request |

Per-request overrides (in the JSON body): `max_cost`, `max_workers`, `timeout_seconds`.

---

## Models

| Model | Input | Output | Used at |
|---|---:|---:|---|
| `deepseek/deepseek-v4-flash:nitro` | $0.055 / M | $0.110 / M | Default workhorse (all stages at cheap/standard) |
| `deepseek/deepseek-v4-pro:nitro` | $0.89 / M | $1.79 / M | Synthesis at standard, all LLM stages at deep |
| `moonshotai/kimi-k2.6:nitro` | $2.00 / M | $8.00 / M | Final synthesis at deep |

`:nitro` routes through OpenRouter's lowest-latency provider tier.

---

## Tests

```bash
# Pure logic only (no API keys, < 1 s)
pytest tests/ -m "not integration"

# Full suite (requires OPENROUTER_API_KEY + SEARXNG running)
pytest
```

138 pure tests pass without keys. 334+ with keys. Zero mocks: integration tests hit real OpenRouter, real SearXNG, real web pages.

---

## Documentation

| Document | What's in it |
|---|---|
| [README.md](./README.md) | This file |
| [CHANGELOG.md](./CHANGELOG.md) | Release-by-release change log |
| [docs/TEAM-BRIEF.md](./docs/TEAM-BRIEF.md) | Team-facing overview: what's built, costs, comparison with the Signal Engine prototype, deployment, next steps |
| [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) | System design, concurrency model, data flow |
| [docs/MODULES.md](./docs/MODULES.md) | Module-level interfaces and dependencies |
| [docs/RULES.md](./docs/RULES.md) | 19 mandatory engineering rules |
| [docs/CLAY.md](./docs/CLAY.md) | Clay column-mapping recipe |
| [AGENTS.md](./AGENTS.md) | Development rules for AI coding agents |

---

## Project layout

```
api.py                       FastAPI transport (thin)
cli.py                       CLI entry point
answer_builder.py            Pure response packing
cache.py                     In-process TTL cache
formatting.py                Date + event formatting helpers
models.py                    UsageStats, CostTracker

core/
  __init__.py
  context.py                 RunContext, TopicState, RunMetrics
  depth.py                   ResearchDepthPolicy + per-model pricing
  pipeline.py                11-stage orchestration
  runner.py                  parallel(fn, items, max_workers)
  types.py                   Shared dataclasses

tools/
  company.py                 Resolve company → canonical aliases
  topic.py                   Topic keyword expansion
  queries.py                 Query generation
  validate.py                "Is company the actor of topic?"
  format.py                  Classify + extract date + summarize

filters/
  dedup.py                   URL + content-hash dedup
  stop_protocol.py           Date + domain + keyword gates
  ranker.py                  Authority + recency scoring
  signals.py                 Soft-signal routing

providers/
  llm.py                     httpx → OpenRouter /v1/chat/completions
  search.py                  httpx → SearXNG
  scrape.py                  httpx + trafilatura + SSRF guard

tests/                       138 pure + 334 with API keys
scripts/
  stress.py                  In-process psutil benchmarks
  stress_http.py             Docker-stats benchmarks

Dockerfile                   Multi-stage Python 3.11-slim, 265 MB
.dockerignore
railway.toml                 Railway deployment config
requirements.txt             Pinned versions
```

---

## License

Proprietary — internal Revenanas tooling.
