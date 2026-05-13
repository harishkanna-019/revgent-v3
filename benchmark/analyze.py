"""Analyze Revgent vs Claygent benchmark results."""

import csv
import json
import re
import sys
from collections import Counter, defaultdict


def parse_claygent_event_count(response: str) -> int:
    """Extract EVENT_COUNT from Claygent response."""
    m = re.search(r"EVENT_COUNT:\s*(\d+)", response)
    return int(m.group(1)) if m else -1


def parse_claygent_cost(cost_str: str) -> float:
    """Parse Claygent cost like '$0.00399'."""
    cost_str = cost_str.strip().replace("$", "").replace(",", "")
    try:
        return float(cost_str)
    except ValueError:
        return 0.0


def analyze(path: str) -> None:
    with open(path) as f:
        rows = list(csv.DictReader(f))

    total = len(rows)
    print(f"Total rows: {total}")
    print(f"Companies: {len(set(r['company_domain'] for r in rows))}")
    print(f"Topics: {dict(Counter(r['topic'] for r in rows))}")
    print()

    # ── Parse both sides ──
    results = []
    for r in rows:
        cg_resp = r.get("Claygent-Response", "").strip()
        cg_count = parse_claygent_event_count(cg_resp)
        cg_cost = parse_claygent_cost(r.get("Total Cost of Claygent", "0"))
        cg_time = float(r.get("Claygent-Time Taken In Seconds", "0") or "0")
        cg_input_tokens = int(r.get("Claygent-Total Input Tokens", "0") or "0")
        cg_output_tokens = int(r.get("Claygent-Total Output Tokens", "0") or "0")

        rg_raw = r.get("Revgent-Output-JSON", "").strip()
        rg = json.loads(rg_raw) if rg_raw else {}
        rg_count = rg.get("event_count", 0)
        rg_signal_count = rg.get("signal_count", 0)
        rg_cost = rg.get("total_cost_usd", 0.0)
        rg_time = rg.get("elapsed_ms", 0) / 1000.0
        rg_tokens = rg.get("total_tokens", 0)
        rg_confidence = rg.get("confidence", "low")
        rg_is_valid = rg.get("is_valid", False)

        # Pipeline trace
        trace = {t["stage"]: t["out"] for t in rg.get("stage_trace", [])}

        results.append({
            "company": r["company_domain"],
            "name": r["company_name"],
            "topic": r["topic"],
            "tier": r["tier"],
            "cg_count": cg_count,
            "cg_cost": cg_cost,
            "cg_time": cg_time,
            "cg_input_tokens": cg_input_tokens,
            "cg_output_tokens": cg_output_tokens,
            "rg_count": rg_count,
            "rg_signal_count": rg_signal_count,
            "rg_cost": rg_cost,
            "rg_time": rg_time,
            "rg_tokens": rg_tokens,
            "rg_confidence": rg_confidence,
            "rg_is_valid": rg_is_valid,
            "rg_search": trace.get("search", 0),
            "rg_dedup": trace.get("dedup", 0),
            "rg_stop": trace.get("stop_protocol", 0),
            "rg_rank": trace.get("rank", 0),
            "rg_scrape": trace.get("scrape", 0),
            "rg_validate": trace.get("validate", 0),
        })

    # ── Detection Rate ──
    print("=" * 70)
    print("DETECTION RATE (found >= 1 event)")
    print("=" * 70)
    cg_found = sum(1 for r in results if r["cg_count"] > 0)
    rg_found = sum(1 for r in results if r["rg_count"] > 0)
    rg_found_or_signal = sum(
        1 for r in results if r["rg_count"] > 0 or r["rg_signal_count"] > 0
    )
    print(f"  Claygent:  {cg_found}/{total} ({cg_found/total*100:.0f}%)")
    print(f"  Revgent:   {rg_found}/{total} ({rg_found/total*100:.0f}%)")
    print(f"  Revgent (events+signals): {rg_found_or_signal}/{total} ({rg_found_or_signal/total*100:.0f}%)")
    print()

    # By topic
    print("Detection by topic:")
    for topic in ["funding round", "C-suite executive changes", "data breach"]:
        subset = [r for r in results if r["topic"] == topic]
        n = len(subset)
        cg = sum(1 for r in subset if r["cg_count"] > 0)
        rg = sum(1 for r in subset if r["rg_count"] > 0)
        rgs = sum(1 for r in subset if r["rg_count"] > 0 or r["rg_signal_count"] > 0)
        print(f"  {topic:<30} CG={cg}/{n} ({cg/n*100:.0f}%)  RG={rg}/{n} ({rg/n*100:.0f}%)  RG+sig={rgs}/{n} ({rgs/n*100:.0f}%)")
    print()

    # By tier
    print("Detection by tier:")
    for tier in ["enterprise", "growth", "mid-market"]:
        subset = [r for r in results if r["tier"] == tier]
        n = len(subset)
        if n == 0:
            continue
        cg = sum(1 for r in subset if r["cg_count"] > 0)
        rg = sum(1 for r in subset if r["rg_count"] > 0)
        print(f"  {tier:<15} CG={cg}/{n} ({cg/n*100:.0f}%)  RG={rg}/{n} ({rg/n*100:.0f}%)")
    print()

    # ── Agreement / Disagreement ──
    print("=" * 70)
    print("AGREEMENT MATRIX")
    print("=" * 70)
    both_found = sum(1 for r in results if r["cg_count"] > 0 and r["rg_count"] > 0)
    cg_only = sum(1 for r in results if r["cg_count"] > 0 and r["rg_count"] == 0)
    rg_only = sum(1 for r in results if r["cg_count"] == 0 and r["rg_count"] > 0)
    neither = sum(1 for r in results if r["cg_count"] == 0 and r["rg_count"] == 0)
    print(f"  Both found events:     {both_found}")
    print(f"  Claygent only:         {cg_only}")
    print(f"  Revgent only:          {rg_only}")
    print(f"  Neither found:         {neither}")
    print()

    # ── Cost Comparison ──
    print("=" * 70)
    print("COST COMPARISON")
    print("=" * 70)
    total_cg_cost = sum(r["cg_cost"] for r in results)
    total_rg_cost = sum(r["rg_cost"] for r in results)
    avg_cg_cost = total_cg_cost / total
    avg_rg_cost = total_rg_cost / total
    print(f"  Claygent total:  ${total_cg_cost:.4f}  (avg ${avg_cg_cost:.5f}/query)")
    print(f"  Revgent total:   ${total_rg_cost:.4f}  (avg ${avg_rg_cost:.5f}/query)")
    if avg_rg_cost > 0:
        print(f"  Claygent/Revgent ratio: {avg_cg_cost/avg_rg_cost:.1f}x")
    print()

    # ── Speed Comparison ──
    print("=" * 70)
    print("SPEED COMPARISON (seconds)")
    print("=" * 70)
    cg_times = sorted(r["cg_time"] for r in results)
    rg_times = sorted(r["rg_time"] for r in results)
    print(f"  Claygent  p50={cg_times[len(cg_times)//2]:.1f}s  p90={cg_times[int(len(cg_times)*0.9)]:.1f}s  avg={sum(cg_times)/len(cg_times):.1f}s")
    print(f"  Revgent   p50={rg_times[len(rg_times)//2]:.1f}s  p90={rg_times[int(len(rg_times)*0.9)]:.1f}s  avg={sum(rg_times)/len(rg_times):.1f}s")
    print()

    # ── Token Usage ──
    print("=" * 70)
    print("TOKEN USAGE")
    print("=" * 70)
    total_cg_tokens = sum(r["cg_input_tokens"] + r["cg_output_tokens"] for r in results)
    total_rg_tokens = sum(r["rg_tokens"] for r in results)
    print(f"  Claygent total tokens: {total_cg_tokens:,}")
    print(f"  Revgent total tokens:  {total_rg_tokens:,}")
    print(f"  Avg per query - CG: {total_cg_tokens/total:,.0f}  RG: {total_rg_tokens/total:,.0f}")
    print()

    # ── Pipeline Bottleneck Analysis ──
    print("=" * 70)
    print("REVGENT PIPELINE BOTTLENECK (where Claygent found but Revgent missed)")
    print("=" * 70)
    misses = [r for r in results if r["cg_count"] > 0 and r["rg_count"] == 0]
    if misses:
        # Categorize the failure point
        no_search = sum(1 for r in misses if r["rg_search"] == 0)
        search_but_no_stop = sum(
            1 for r in misses if r["rg_search"] > 0 and r["rg_stop"] == 0
        )
        stop_but_no_scrape = sum(
            1 for r in misses if r["rg_stop"] > 0 and r["rg_scrape"] == 0
        )
        scrape_but_no_validate = sum(
            1 for r in misses if r["rg_scrape"] > 0 and r["rg_validate"] == 0
        )
        validate_but_no_event = sum(
            1 for r in misses if r["rg_validate"] > 0 and r["rg_count"] == 0
        )

        print(f"  Total misses (CG found, RG missed): {len(misses)}")
        print(f"  No search results:                   {no_search}")
        print(f"  Search OK but stop_protocol killed:   {search_but_no_stop}")
        print(f"  Stop OK but not scraped:              {stop_but_no_scrape}")
        print(f"  Scraped but validation rejected ALL:  {scrape_but_no_validate}")
        print(f"  Validated but no event extracted:     {validate_but_no_event}")
        print()

        # Avg pipeline counts for misses
        avg_search = sum(r["rg_search"] for r in misses) / len(misses)
        avg_dedup = sum(r["rg_dedup"] for r in misses) / len(misses)
        avg_stop = sum(r["rg_stop"] for r in misses) / len(misses)
        avg_scrape = sum(r["rg_scrape"] for r in misses) / len(misses)
        avg_validate = sum(r["rg_validate"] for r in misses) / len(misses)
        print(f"  Avg pipeline for misses: search={avg_search:.0f} dedup={avg_dedup:.0f} stop={avg_stop:.0f} scrape={avg_scrape:.0f} validate={avg_validate:.1f}")
    print()

    # ── Detailed Miss List ──
    print("=" * 70)
    print("DETAILED MISS LIST (Claygent found, Revgent missed)")
    print("=" * 70)
    for r in misses[:30]:
        failure = "no_search"
        if r["rg_search"] > 0 and r["rg_stop"] == 0:
            failure = "stop_protocol"
        elif r["rg_stop"] > 0 and r["rg_scrape"] == 0:
            failure = "no_scrape"
        elif r["rg_scrape"] > 0 and r["rg_validate"] == 0:
            failure = "VALIDATION"
        elif r["rg_validate"] > 0:
            failure = "format_route"
        print(
            f"  {r['company']:<25} {r['topic']:<30} CG={r['cg_count']} "
            f"pipeline: {r['rg_search']}->{r['rg_dedup']}->{r['rg_stop']}->"
            f"{r['rg_scrape']}->{r['rg_validate']}  FAIL={failure}"
        )
    if len(misses) > 30:
        print(f"  ... and {len(misses) - 30} more")
    print()

    # ── Both Found Nothing ──
    print("=" * 70)
    print("BOTH AGREE: NO EVENTS (true negatives?)")
    print("=" * 70)
    neither_list = [r for r in results if r["cg_count"] == 0 and r["rg_count"] == 0]
    for topic in ["funding round", "C-suite executive changes", "data breach"]:
        subset = [r for r in neither_list if r["topic"] == topic]
        names = [r["company"] for r in subset]
        print(f"  {topic}: {len(subset)} companies")
        if names:
            print(f"    {', '.join(names[:10])}")
            if len(names) > 10:
                print(f"    ... and {len(names)-10} more")
    print()

    # ── Summary Scorecard ──
    print("=" * 70)
    print("SCORECARD SUMMARY")
    print("=" * 70)
    print(f"  {'Metric':<35} {'Claygent':>12} {'Revgent':>12}")
    print(f"  {'-'*35} {'-'*12} {'-'*12}")
    print(f"  {'Detection rate':<35} {cg_found/total*100:>11.0f}% {rg_found/total*100:>11.0f}%")
    print(f"  {'Avg cost/query':<35} ${avg_cg_cost:>10.5f} ${avg_rg_cost:>10.5f}")
    print(f"  {'Avg latency (s)':<35} {sum(cg_times)/len(cg_times):>11.1f}s {sum(rg_times)/len(rg_times):>11.1f}s")
    print(f"  {'Avg tokens/query':<35} {total_cg_tokens/total:>11,.0f} {total_rg_tokens/total:>11,.0f}")
    print(f"  {'Total cost (150 queries)':<35} ${total_cg_cost:>10.4f} ${total_rg_cost:>10.4f}")
    print()
    print("  NOTE: Revgent detection rate is low due to overly strict validation.")
    print("  The validation LLM rejects articles that SearXNG finds and scrapes.")
    print("  Pipeline shows search->scrape works, but validate->0 is the bottleneck.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "benchmark/Companies Default View Export.csv"
    analyze(path)
