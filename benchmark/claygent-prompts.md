# Revgent vs Claygent Benchmark

## Table Structure

Import `companies.csv` into Clay. Columns:

| Column | Source | Type |
|--------|--------|------|
| `company_domain` | CSV | Text |
| `company_name` | CSV | Text |
| `tier` | CSV | Text |
| `industry` | CSV | Text |
| `topic` | CSV | Text |
| `Claygent Result` | Claygent enrichment | Text |
| `Claygent Event Count` | Formula (parse from Claygent) | Number |
| `Revgent Result` | HTTP API enrichment | Text |
| `Revgent Event Count` | Formula (parse from Revgent) | Number |

150 rows total (50 companies x 3 topics).

---

## Claygent Column

Single Claygent enrichment that works for all 3 signal types using the `topic` column.

### Prompt

```
Research whether {company_name} ({company_domain}) has experienced any {topic} in the past 12 months.

Guidelines by topic type:
- "funding round": Include Series A/B/C/D rounds, IPOs, SPACs, secondary sales, debt financing. Report the amount, round stage, lead investors.
- "C-suite executive changes": Include CEO, CTO, CFO, COO, CRO, CMO, CPO, CISO appointments, departures, promotions, interim appointments.
- "data breach": Include confirmed breaches, ransomware attacks, data leaks, disclosed vulnerabilities, regulatory actions related to data protection. Report the scale (records/users affected) if known.

For each event found, provide:
- Date (YYYY-MM-DD format, be as precise as possible)
- One-sentence description with key details
- Source URL

Format your response EXACTLY as:
EVENT_COUNT: [number]
---
[YYYY-MM-DD] [one-sentence description] | Source: [URL]
---

If nothing found, respond EXACTLY as:
EVENT_COUNT: 0
No {topic} found for {company_name} in the past 12 months.
```

---

## Revgent Column (HTTP API)

### Method
`POST`

### URL
```
https://web-production-16783.up.railway.app/research/clay
```

### Headers
```
Content-Type: application/json
X-Api-Key: 83fab2cb27cea9012b0a6aea6399936c
```

### Body
```json
{
  "company": "{{company_domain}}",
  "topics": ["{{topic}}"],
  "depth": "standard"
}
```

### Response Mapping

Map these fields into Clay columns:

| Clay Column | Response Path | Type |
|-------------|---------------|------|
| Revgent Event Count | `event_count` | Number |
| Revgent Signal Count | `signal_count` | Number |
| Revgent Valid | `is_valid` | Boolean |
| Revgent Confidence | `confidence` | Text |
| Revgent Summary | `summary` | Text |
| Revgent Headline | `primary_headline` | Text |
| Revgent Source | `primary_source_url` | URL |
| Revgent Date | `primary_date` | Text |
| Revgent Cost | `total_cost_usd` | Number |
| Revgent Tokens | `total_tokens` | Number |
| Revgent Elapsed ms | `elapsed_ms` | Number |
| Revgent Queries | `queries_used` | Text |

---

## Evaluation Columns (add after both run)

Add these formula/manual columns for scoring:

| Column | How to Fill |
|--------|-------------|
| `Ground Truth Event Count` | Manual research - actual number of events that happened |
| `Claygent Precision` | Manual - what % of Claygent events are real |
| `Revgent Precision` | Manual - what % of Revgent events are real |
| `Claygent Date Accurate` | Manual - are dates correct (yes/partial/no) |
| `Revgent Date Accurate` | Manual - are dates correct (yes/partial/no) |
| `Claygent Has Source` | Formula - does response contain a URL |
| `Revgent Has Source` | Already mapped (`primary_source_url`) |
| `Winner` | Manual - which gave better result (claygent/revgent/tie) |
| `Notes` | Manual - qualitative observations |

---

## Running the Benchmark

1. Import `companies.csv` into a new Clay table
2. Add the Claygent column with the prompt above
3. Add the Revgent HTTP API column with the config above
4. Run both enrichments on all 150 rows
5. Fill in ground truth + evaluation columns
6. Export final table as CSV to `benchmark/results.csv`
