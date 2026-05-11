# Clay Integration

How to call Revgent v3 from a Clay table.

## TL;DR

1. Deploy this repo to Railway (or expose locally via a tunnel for testing).
2. In Clay, add an **HTTP API** column.
3. POST to `https://<your-host>/research/clay` with the company domain and
   topic from your row.
4. Map the flat response fields directly to Clay columns.

That's it. Below is the detail.

---

## 1. Endpoints

| Endpoint | Use case |
|---|---|
| `GET /` | Health check (always public). Returns `{"status":"ok"}`. |
| `POST /research` | Full v2-shaped response. Nested arrays for events / signals / answers. |
| `POST /research/clay` | **Recommended for Clay.** Same data but with flat top-level convenience fields. |
| `POST /research/async` | Fire-and-forget; result POSTed to a `webhook_url` you provide. Use for very large Clay batches. |

## 2. Auth (optional)

Set `REVGENT_API_KEY=<random-secret>` as an env var on Railway. Then every
request must include header `X-Api-Key: <random-secret>`. If the env var is
unset, the API is open.

## 3. Request shape

```json
{
  "company": "meta.com",
  "topics": ["layoffs"],
  "depth": "cheap",
  "max_cost": 0.01,
  "date_min": 0,
  "date_max": 90
}
```

Field notes:
- `company` accepts the v2-alias `company_domain` too — pick whichever Clay column you have.
- `topics` is a list. Clay can templated this via `[{{ topic }}]` mustache.
- `depth` is `cheap | standard | deep`.
- `max_cost` is optional. Capped at `$5`.
- `date_min` / `date_max` are days-ago.

## 4. Response (`/research/clay`)

```json
{
  "company": "meta.com",
  "topic": "layoffs",
  "event_count": 7,
  "signal_count": 3,
  "is_valid": true,
  "confidence": "high",
  "summary": "Meta laid off ~600 staff in the AI division on 2026-05-02 ...",
  "primary_headline": "Meta layoffs: More cuts possible after 10% workforce reduction",
  "primary_source_url": "https://...",
  "primary_source_name": "msn.com",
  "primary_date": "2026-05-02",
  "signal_type": "",
  "signal_confidence": 0.0,
  "total_cost_usd": 0.000851,
  "total_tokens": 10731,
  "events":   [ ... full array ... ],
  "signals":  [ ... full array ... ],
  "answers":  [ ... full array ... ]
}
```

Every field starting at `company` through `total_tokens` is a flat scalar Clay
can map directly. The `events / signals / answers` arrays at the bottom are
there if you need to drill in.

## 5. Clay column config

In Clay, add a column → **HTTP API**:

| Field | Value |
|---|---|
| **Method** | `POST` |
| **URL** | `https://<your-host>/research/clay` |
| **Headers** | `Content-Type: application/json` + `X-Api-Key: <secret>` if you set one |
| **Body** | (see below) |
| **Auth** | None (we use header key) |
| **Output** | Pick fields via Clay's response mapper. |

### Body template (Clay mustache)

```json
{
  "company": "{{ company_domain }}",
  "topics": ["{{ research_topic }}"],
  "depth": "cheap"
}
```

Use `{{ column_name }}` for whatever column holds the domain. For multiple
topics per row, change the topics array.

### Mapping fields back into Clay columns

Click "use AI to format" or use Clay's response-mapper UI and pick:

- `summary` → "Research Summary" column
- `is_valid` → boolean
- `confidence` → text
- `primary_source_url` → URL
- `primary_headline` → text
- `total_cost_usd` → number (for monitoring)

## 6. Depth recommendations for Clay batches

| Workload | Depth | Cost per row | Latency p50 | Notes |
|---|---|---:|---:|---|
| Bulk pre-qualify, 10k+ rows | `cheap` | ~$0.00004 | ~10 s | Snippet-only, no scraping. Surfaces signals only. |
| Standard enrichment, 1k–10k rows | `standard` | ~$0.00015 | ~25 s | Scrapes top 5, full LLM summaries. **Best default.** |
| Deep dive, ≤500 rows | `deep` | ~$0.0007 | ~60 s | Scrapes top 20, uses Kimi for summarization. Use sparingly. |

At standard depth, 10k Clay rows cost **~$1.50**.

## 7. Rate limits & concurrency

- Clay processes rows in batches; the default concurrency is around 10.
- The service handles 20 concurrent standard-depth requests on a Railway
  Starter plan (512 MB / 1 CPU) at ~167 MB peak RSS.
- For very large jobs (≥1000 concurrent), use `/research/async` with a
  webhook so Clay isn't blocked waiting on each row.

## 8. Local testing before Railway deploy

Expose your local Docker container to the internet:

```bash
# Run the container
docker build -t revgent-v3:test .
docker run -d --name revgent-test \
  -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
  -e SEARXNG_URL=http://host.docker.internal:8888 \
  -e REVGENT_API_KEY=$(openssl rand -hex 16) \
  -p 8765:8000 revgent-v3:test

# Tunnel it
cloudflared tunnel --url http://localhost:8765
# or
ngrok http 8765
```

Cloudflared/ngrok will print a public HTTPS URL (e.g.
`https://random-name.trycloudflare.com`). Point Clay at that URL plus the
random API key. **Use this only for testing** — your laptop must stay
online and SearXNG must be running.

## 9. Production deploy

```bash
railway login
railway init
railway up
```

Set in the Railway dashboard:

| Variable | Required | Value |
|---|---|---|
| `OPENROUTER_API_KEY` | yes | Your OpenRouter key |
| `SEARXNG_URL` | yes | URL of your SearXNG (deploy as a sibling service or use a managed instance) |
| `REVGENT_API_KEY` | recommended | Random 32-byte hex string |
| `LLM_CONCURRENCY` | no | `12` default |
| `SEARCH_CONCURRENCY` | no | `8` default |
| `SCRAPE_CONCURRENCY` | no | `4` default |
| `ABSOLUTE_MAX_COST` | no | `5.0` default |

Railway exposes the service at a `*.up.railway.app` URL. Use that URL in
the Clay column config.
