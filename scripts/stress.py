"""Stress test: measure latency, CPU%, RSS for cheap and standard depth.

Runs N concurrent requests against the real OpenRouter + SearXNG, samples
process CPU and memory at 100ms intervals throughout the run, then reports
percentiles and totals.

Usage:
    python scripts/stress.py --depth cheap --concurrency 10
    python scripts/stress.py --depth standard --concurrency 5
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import threading
import time
from dataclasses import dataclass, field

import psutil

# Load .env if present so OPENROUTER_API_KEY / SEARXNG_URL are visible
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from core.context import RunContext
from core.depth import ResearchDepthPolicy
from core.pipeline import run as pipeline_run
from providers import llm, scrape, search

# A small but realistic ICP-style mix
COMPANIES = [
    "meta.com",
    "google.com",
    "openai.com",
    "anthropic.com",
    "stripe.com",
    "shopify.com",
    "airbnb.com",
    "uber.com",
    "lyft.com",
    "doordash.com",
    "notion.so",
    "linear.app",
    "figma.com",
    "vercel.com",
    "supabase.com",
    "railway.app",
    "render.com",
    "cloudflare.com",
    "datadog.com",
    "snowflake.com",
]

TOPICS = ["layoffs"]


@dataclass
class RunResult:
    company: str
    ok: bool
    elapsed_s: float
    events: int = 0
    signals: int = 0
    cost: float = 0.0
    tokens: int = 0
    error: str = ""


@dataclass
class Sampler:
    """Background thread that samples process metrics every 100ms."""

    interval_s: float = 0.1
    samples: list[tuple[float, float, float]] = field(default_factory=list)
    # (timestamp_s, cpu_pct, rss_mb)
    _stop: threading.Event = field(default_factory=threading.Event)
    _proc: psutil.Process = field(default_factory=lambda: psutil.Process())

    def start(self) -> None:
        # Prime CPU% (first call returns 0)
        self._proc.cpu_percent(interval=None)
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        self._t.join()

    def _loop(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic() - t0
            cpu = self._proc.cpu_percent(interval=None)
            mem = self._proc.memory_info().rss / 1024 / 1024
            self.samples.append((now, cpu, mem))
            time.sleep(self.interval_s)


async def one_request(company: str, topic: str, depth: str) -> RunResult:
    policy = ResearchDepthPolicy.from_request(depth)
    ctx = RunContext(
        policy=policy,
        company=company,
        topics=[topic],
        date_min=0,
        date_max=90,
    )
    t0 = time.monotonic()
    try:
        resp = await pipeline_run(ctx)
        elapsed = time.monotonic() - t0
        return RunResult(
            company=company,
            ok=True,
            elapsed_s=elapsed,
            events=len(resp.get("events", [])),
            signals=len(resp.get("signals", [])),
            cost=resp.get("cost", {}).get("total_cost", 0.0),
            tokens=resp.get("usage", {}).get("total_tokens", 0),
        )
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return RunResult(
            company=company,
            ok=False,
            elapsed_s=elapsed,
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


async def main(depth: str, concurrency: int) -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not set; aborting stress test")
        return 1

    await llm.init()
    await search.init()
    await scrape.init()

    # Pick concurrency-many companies (cycle if more requested)
    chosen = [COMPANIES[i % len(COMPANIES)] for i in range(concurrency)]

    proc = psutil.Process()
    baseline_rss = proc.memory_info().rss / 1024 / 1024
    print(f"baseline RSS after init: {baseline_rss:.1f} MB")

    sampler = Sampler(interval_s=0.1)
    sampler.start()

    wall_start = time.monotonic()
    results = await asyncio.gather(*[one_request(c, TOPICS[0], depth) for c in chosen])
    wall_elapsed = time.monotonic() - wall_start

    sampler.stop()

    await llm.close()
    await search.close()
    await scrape.close()

    # ── Report ──
    ok = [r for r in results if r.ok]
    fail = [r for r in results if not r.ok]
    latencies = [r.elapsed_s for r in ok]
    cpu_samples = [s[1] for s in sampler.samples]
    mem_samples = [s[2] for s in sampler.samples]

    print()
    print("=" * 64)
    print(f"  STRESS TEST: depth={depth} concurrency={concurrency}")
    print("=" * 64)
    print(f"wall clock           : {wall_elapsed:.1f} s")
    print(f"successful requests  : {len(ok)} / {concurrency}")
    if fail:
        print(f"failures             : {len(fail)}")
        for r in fail[:3]:
            print(f"    {r.company:<20} {r.error}")
    if latencies:
        print()
        print(f"latency per request  (n={len(latencies)})")
        print(f"  min                : {min(latencies):>6.2f} s")
        print(f"  p50                : {pct(latencies, 50):>6.2f} s")
        print(f"  p90                : {pct(latencies, 90):>6.2f} s")
        print(f"  p99                : {pct(latencies, 99):>6.2f} s")
        print(f"  max                : {max(latencies):>6.2f} s")
        print(f"  mean               : {statistics.mean(latencies):>6.2f} s")
    if cpu_samples:
        print()
        print(f"CPU usage  (samples={len(cpu_samples)} @ 100ms)")
        print(f"  mean               : {statistics.mean(cpu_samples):>6.1f} %")
        print(f"  p90                : {pct(cpu_samples, 90):>6.1f} %")
        print(f"  peak               : {max(cpu_samples):>6.1f} %")
    if mem_samples:
        print()
        print(f"RSS memory  (samples={len(mem_samples)})")
        print(f"  baseline           : {baseline_rss:>6.1f} MB")
        print(f"  mean               : {statistics.mean(mem_samples):>6.1f} MB")
        print(f"  peak               : {max(mem_samples):>6.1f} MB")
        print(f"  delta peak-baseline: {max(mem_samples) - baseline_rss:>6.1f} MB")

    if ok:
        total_cost = sum(r.cost for r in ok)
        total_tokens = sum(r.tokens for r in ok)
        total_events = sum(r.events for r in ok)
        total_signals = sum(r.signals for r in ok)
        print()
        print("workload totals")
        print(f"  events             : {total_events}")
        print(f"  signals            : {total_signals}")
        print(f"  tokens             : {total_tokens:,}")
        print(f"  cost (USD)         : ${total_cost:.4f}")
        print(f"  cost per request   : ${total_cost / len(ok):.6f}")
        print(f"  throughput         : {len(ok) / wall_elapsed:.2f} req/s")

    print("=" * 64)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--depth", choices=["cheap", "standard", "deep"], default="cheap"
    )
    parser.add_argument("--concurrency", type=int, default=10)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.depth, args.concurrency)))
