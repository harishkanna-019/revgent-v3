#!/bin/bash
set -euo pipefail

# Revgent recall benchmark: 10 company/topic pairs with known ground truth.
# Runs the pipeline locally, compares to expected events, outputs metrics.

cd "$(dirname "$0")"

python3 << 'PYEOF'
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv(dotenv_path=".env")

from core.context import RunContext
from core.depth import ResearchDepthPolicy
from core.pipeline import run
from providers import llm, search, scrape


# Ground truth: 12 company/topic pairs. Broader mix to avoid overfitting.
# Includes easy, medium, hard, and true negatives across all 3 topics.
GROUND_TRUTH = [
    # Easy TPs (well-indexed events)
    ("databricks.com", "funding round", True),
    ("canva.com", "C-suite executive changes", True),
    ("okta.com", "data breach", True),
    # Medium TPs (need good queries)
    ("ramp.com", "funding round", True),
    ("vercel.com", "data breach", True),
    ("coupang.com", "C-suite executive changes", True),
    # Hard TPs (validation-sensitive or niche events)
    ("stripe.com", "funding round", True),
    ("anthropic.com", "data breach", True),
    ("6sense.com", "C-suite executive changes", True),
    # True negatives (must NOT find events)
    ("cloudflare.com", "funding round", False),
    ("canva.com", "funding round", False),
    ("mongodb.com", "data breach", False),
]


async def run_benchmark():
    await llm.init()
    await search.init()
    await scrape.init()

    tp = 0
    fp = 0
    fn = 0
    tn = 0
    total_events = 0
    total_latency_ms = 0
    total_cost = 0.0

    for company, topic, has_event in GROUND_TRUTH:
        t0 = time.monotonic()
        policy = ResearchDepthPolicy.from_request("standard", max_cost=0.50)
        ctx = RunContext(
            policy=policy,
            company=company,
            topics=[topic],
            date_min=0,
            date_max=365,
            strict_date=False,
        )

        try:
            result = await run(ctx, timeout_seconds=90.0)
        except Exception as e:
            print(f"  ERROR: {company}/{topic}: {e}", file=sys.stderr)
            if has_event:
                fn += 1
            else:
                tn += 1
            continue

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        total_latency_ms += elapsed_ms

        events = result.get("events", [])
        found = len(events) > 0
        total_events += len(events)
        total_cost += result.get("cost", {}).get("total_cost", 0.0)

        if has_event:
            if found:
                tp += 1
                status = "TP"
            else:
                fn += 1
                status = "FN"
        else:
            if found:
                fp += 1
                status = "FP"
            else:
                tn += 1
                status = "TN"

        print(f"  {status} {company:25s} {topic:30s} events={len(events)} {elapsed_ms}ms", file=sys.stderr)

        # 1s delay between requests to avoid engine suspension
        await asyncio.sleep(1)

    await scrape.close()
    await search.close()
    await llm.close()

    # Compute metrics
    total = tp + fp + fn + tn
    recall = tp / max(tp + fn, 1) * 100
    precision = tp / max(tp + fp, 1) * 100
    f1 = 2 * precision * recall / max(precision + recall, 1)
    avg_latency = total_latency_ms / max(total, 1)

    print(f"METRIC recall_pct={recall:.1f}")
    print(f"METRIC precision_pct={precision:.1f}")
    print(f"METRIC f1_pct={f1:.1f}")
    print(f"METRIC events_found={total_events}")
    print(f"METRIC false_positives={fp}")
    print(f"METRIC avg_latency_ms={avg_latency:.0f}")
    print(f"METRIC total_cost_usd={total_cost:.4f}")

    # Summary
    print(f"", file=sys.stderr)
    print(f"TP={tp} FP={fp} FN={fn} TN={tn}", file=sys.stderr)
    print(f"Recall={recall:.1f}% Precision={precision:.1f}% F1={f1:.1f}%", file=sys.stderr)


asyncio.run(run_benchmark())
PYEOF
