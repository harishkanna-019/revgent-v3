"""Stress-test SearXNG engine combinations to find rate limits.

Usage:
  python3 scripts/stress_engines.py [--engines "bing news,google"] [--concurrent N] [--total N]

Fires concurrent search requests directly to SearXNG, measures:
- Success rate (returned results > 0)
- Error rate (HTTP 403, 429, blocks)
- Timeout rate
- Results per engine
"""

import asyncio
import httpx
import time
import sys
import json
from dataclasses import dataclass, field

SEARXNG_URL = "https://searxng-production-bffe.up.railway.app"

# Real company+topic pairs from the benchmark
TEST_QUERIES = [
    ("rippling", "series G funding round raised 2025"),
    ("airtable", "CTO appointed chief technology officer 2025"),
    ("deel", "series E funding round raised 2025"),
    ("stripe", "tender offer valuation 2025 2026"),
    ("cloudflare", "convertible notes offering 2025"),
    ("notion", "employee tender offer valuation 2025"),
    ("canva", "secondary share sale valuation 2025"),
    ("clay", "series C funding round raised 2025"),
    ("anthropic", "series F funding round 2025"),
    ("anduril", "series G series H funding raised 2025"),
    ("zscaler", "CFO appointed hired 2025"),
    ("mongodb", "CEO appointed hired 2025 2026"),
    ("hubspot", "CPTO chief product technology officer 2025"),
    ("lattice", "CRO chief revenue officer hired 2026"),
    ("apollo.io", "CEO appointed 2026"),
    ("meta", "Dina Powell McCormick president appointed 2026"),
    ("snowflake", "CFO appointed hired 2025"),
    ("cohere", "CFO appointed hired 2025"),
    ("scale", "interim CEO appointed 2025"),
    ("figma", "IPO NYSE 2025"),
]

@dataclass
class Result:
    query: str
    engine_set: str
    ok: bool = False
    status_code: int = 0
    result_count: int = 0
    elapsed_ms: float = 0
    error: str = ""


async def search_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                     company: str, topic: str, engines: str) -> Result:
    q = f'"{company}" {topic}'
    url = (
        f"{SEARXNG_URL}/search?format=json&q={q}"
        f"&time_range=year"
    )
    if engines:
        url += f"&engines={engines}"

    t0 = time.monotonic()
    async with sem:
        try:
            resp = await client.get(url, timeout=15.0)
            elapsed = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                data = resp.json()
                return Result(q, engines, ok=True, status_code=200,
                              result_count=len(data.get("results", [])),
                              elapsed_ms=elapsed)
            elif resp.status_code == 429:
                return Result(q, engines, status_code=429,
                              error="rate limited (429)", elapsed_ms=elapsed)
            elif resp.status_code == 403:
                return Result(q, engines, status_code=403,
                              error="forbidden (403)", elapsed_ms=elapsed)
            else:
                return Result(q, engines, status_code=resp.status_code,
                              error=f"HTTP {resp.status_code}", elapsed_ms=elapsed)
        except httpx.TimeoutException:
            return Result(q, engines, error="timeout",
                          elapsed_ms=(time.monotonic() - t0) * 1000)
        except Exception as e:
            return Result(q, engines, error=str(e)[:80],
                          elapsed_ms=(time.monotonic() - t0) * 1000)


async def run_engine_test(engines: str, label: str, concurrent: int, total_requests: int):
    """Run a batch of queries against a specific engine set."""
    print(f"\n{'='*70}")
    print(f"TEST: {label}")
    print(f"Engines: {engines or 'default (all news via categories=news)'}")
    print(f"Concurrent: {concurrent}, Total: {total_requests}")
    print(f"{'='*70}")

    sem = asyncio.Semaphore(concurrent)
    results: list[Result] = []

    async with httpx.AsyncClient(http2=True) as client:
        tasks = []
        for i in range(total_requests):
            c, t = TEST_QUERIES[i % len(TEST_QUERIES)]
            tasks.append(search_one(client, sem, c, t, engines))

        t0 = time.monotonic()
        results = await asyncio.gather(*tasks)
        total_elapsed = (time.monotonic() - t0)

    ok = [r for r in results if r.ok]
    errs = [r for r in results if not r.ok]
    zero_results = [r for r in ok if r.result_count == 0]

    print(f"\nResults:")
    print(f"  Successful (with results): {len(ok) - len(zero_results)}/{total_requests}")
    print(f"  Successful (zero results): {len(zero_results)}/{total_requests}")
    print(f"  Errors: {len(errs)}/{total_requests}")
    if errs:
        error_types = {}
        for r in errs:
            error_types[r.error] = error_types.get(r.error, 0) + 1
        for err, count in sorted(error_types.items()):
            print(f"    {err}: {count}")

    times = sorted([r.elapsed_ms for r in ok])
    if times:
        p50 = times[len(times)//2]
        p90 = times[int(len(times)*0.9)]
        p99 = times[int(len(times)*0.99)]
        print(f"  Latency p50: {p50:.0f}ms  p90: {p90:.0f}ms  p99: {p99:.0f}ms")
        avg_results = sum(r.result_count for r in ok) / max(len(ok), 1)
        print(f"  Avg results per query: {avg_results:.1f}")

    print(f"  Total wall time: {total_elapsed:.1f}s")

    # Show sample results
    sample_ok = [r for r in ok if r.result_count > 0]
    if sample_ok:
        print(f"\n  Sample (first 3):")
        for r in sample_ok[:3]:
            print(f"    [{r.result_count} results] {r.query[:80]}")

    return results


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrent", type=int, default=10)
    parser.add_argument("--total", type=int, default=60)
    parser.add_argument("--engines-only", type=str,
                        default="bing%20news,google,brave")
    args = parser.parse_args()

    print(f"SearXNG stress test - {SEARXNG_URL}")
    print(f"Concurrency: {args.concurrent}, Total per test: {args.total}")
    print(f"Test queries: {len(TEST_QUERIES)} unique company+topic pairs")

    # Test 1: Current approach (categories=news, all 17 engines)
    await run_engine_test(
        engines="",
        label="BASELINE (categories=news, ~17 engines)",
        concurrent=args.concurrent,
        total_requests=args.total,
    )

    # Small delay between tests
    await asyncio.sleep(3)

    # Test 2: Proposed approach (3 engines only)
    await run_engine_test(
        engines=args.engines_only,
        label=f"PROPOSED (engines={args.engines_only})",
        concurrent=args.concurrent,
        total_requests=args.total,
    )

    # Test 3: Bing only (safest)
    await asyncio.sleep(3)
    await run_engine_test(
        engines="bing%20news",
        label="BING ONLY (safest, time_range supported)",
        concurrent=args.concurrent,
        total_requests=args.total,
    )


if __name__ == "__main__":
    asyncio.run(main())
