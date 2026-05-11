"""Stress test against a running HTTP endpoint, monitoring Docker container.

Usage:
    python scripts/stress_http.py --url http://localhost:8765 --depth cheap --concurrency 10 --container revgent-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import subprocess
import threading
import time
from dataclasses import dataclass, field

import httpx


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


@dataclass
class DockerSampler:
    """Sample docker stats every interval_s seconds in a background thread."""

    container: str
    interval_s: float = 0.5
    samples: list[tuple[float, float, float]] = field(default_factory=list)
    # (t_relative, cpu_pct, mem_mib)
    _stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def stop(self) -> None:
        self._stop.set()
        self._t.join()

    def _loop(self) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            try:
                out = subprocess.check_output(
                    [
                        "docker",
                        "stats",
                        "--no-stream",
                        "--format",
                        '{"cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}"}',
                        self.container,
                    ],
                    text=True,
                    timeout=2,
                ).strip()
                row = json.loads(out)
                cpu = float(row["cpu"].rstrip("%"))
                mem_str = row["mem"].split("/")[0].strip()  # e.g. "61.08MiB"
                if mem_str.endswith("MiB"):
                    mem = float(mem_str.replace("MiB", ""))
                elif mem_str.endswith("GiB"):
                    mem = float(mem_str.replace("GiB", "")) * 1024
                elif mem_str.endswith("KiB"):
                    mem = float(mem_str.replace("KiB", "")) / 1024
                else:
                    mem = 0.0
                t = time.monotonic() - t0
                self.samples.append((t, cpu, mem))
            except Exception:
                pass
            self._stop.wait(self.interval_s)


async def one_request(
    client: httpx.AsyncClient, url: str, company: str, depth: str
) -> RunResult:
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{url}/research",
            json={"company": company, "topics": ["layoffs"], "depth": depth},
            timeout=120.0,
        )
        elapsed = time.monotonic() - t0
        if r.status_code != 200:
            return RunResult(
                company=company,
                ok=False,
                elapsed_s=elapsed,
                error=f"HTTP {r.status_code}: {r.text[:100]}",
            )
        d = r.json()
        return RunResult(
            company=company,
            ok=True,
            elapsed_s=elapsed,
            events=len(d.get("events", [])),
            signals=len(d.get("signals", [])),
            cost=d.get("cost", {}).get("total_cost", 0.0),
            tokens=d.get("usage", {}).get("total_tokens", 0),
        )
    except Exception as exc:
        return RunResult(
            company=company,
            ok=False,
            elapsed_s=time.monotonic() - t0,
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )


def pct(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


async def main(url: str, depth: str, concurrency: int, container: str | None) -> int:
    chosen = [COMPANIES[i % len(COMPANIES)] for i in range(concurrency)]

    sampler = DockerSampler(container, interval_s=0.5) if container else None
    if sampler:
        sampler.start()

    wall_start = time.monotonic()
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *[one_request(client, url, c, depth) for c in chosen]
        )
    wall_elapsed = time.monotonic() - wall_start

    if sampler:
        sampler.stop()

    ok = [r for r in results if r.ok]
    fail = [r for r in results if not r.ok]
    latencies = [r.elapsed_s for r in ok]

    print()
    print("=" * 64)
    print(f"  HTTP STRESS: url={url} depth={depth} concurrency={concurrency}")
    print("=" * 64)
    print(f"wall clock           : {wall_elapsed:.1f} s")
    print(f"successful           : {len(ok)} / {concurrency}")
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

    if sampler and sampler.samples:
        cpus = [s[1] for s in sampler.samples]
        mems = [s[2] for s in sampler.samples]
        print()
        print(f"container metrics    (n={len(sampler.samples)} @ 500ms)")
        print(f"  cpu mean           : {statistics.mean(cpus):>6.1f} %  of 1 CPU")
        print(f"  cpu p90            : {pct(cpus, 90):>6.1f} %")
        print(f"  cpu peak           : {max(cpus):>6.1f} %")
        print(f"  mem mean           : {statistics.mean(mems):>6.1f} MiB")
        print(f"  mem peak           : {max(mems):>6.1f} MiB")

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
        print(f"  cost / request     : ${total_cost / len(ok):.6f}")
        print(f"  throughput         : {len(ok) / wall_elapsed:.2f} req/s")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8765")
    parser.add_argument(
        "--depth", choices=["cheap", "standard", "deep"], default="cheap"
    )
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument(
        "--container", default=None, help="Container name to sample via docker stats"
    )
    args = parser.parse_args()
    raise SystemExit(
        asyncio.run(main(args.url, args.depth, args.concurrency, args.container))
    )
