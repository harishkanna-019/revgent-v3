"""CLI entry point for local testing.

Usage:
    python cli.py <domain> <topic> [--depth cheap|standard|deep]

Example:
    python cli.py meta.com layoffs --depth cheap
"""

import argparse
import asyncio
import json
import sys

from core.context import RunContext
from core.depth import ResearchDepthPolicy
from core.pipeline import run
from core.types import Event
from providers import llm, search, scrape


def _render_event(ev: Event) -> str:
    """Render a pipeline event for CLI display."""
    if hasattr(ev, "stage") and hasattr(ev, "count"):
        return f"  [start] {ev.stage}: {ev.count} items"
    if hasattr(ev, "stage") and hasattr(ev, "out"):
        return f"  [end]   {ev.stage}: {ev.out} results"
    if hasattr(ev, "stage") and hasattr(ev, "item_id"):
        return f"  - {ev.item_id[:60]:<60} -> {ev.status}"  # type: ignore[union-attr]
    if hasattr(ev, "spent"):
        return f"  $ spent=${ev.spent:.6f} remaining=${ev.remaining:.6f}"
    return f"  {ev}"


async def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Revgent v3 - Async research agent (CLI)",
    )
    parser.add_argument("domain", help="Company domain (e.g., meta.com)")
    parser.add_argument("topic", help="Research topic (e.g., layoffs)")
    parser.add_argument(
        "--depth",
        choices=["cheap", "standard", "deep"],
        default="cheap",
        help="Research depth (default: cheap)",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="Maximum USD cost (default: depth-dependent)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print pipeline stage events",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="Output raw JSON response",
    )
    args = parser.parse_args()

    # Initialize providers
    await llm.init()
    await search.init()
    await scrape.init()

    try:
        policy = ResearchDepthPolicy.from_request(args.depth, max_cost=args.max_cost)
        ctx = RunContext(
            policy=policy,
            company=args.domain,
            topics=[args.topic],
            date_min=0,
            date_max=90,
        )

        if args.verbose:

            def emit(ev: Event) -> None:
                print(_render_event(ev), file=sys.stderr)
        else:
            emit = None

        response = await run(ctx, emit=emit)

        if args.json:
            print(json.dumps(response, indent=2, default=str))
        else:
            # Human-readable summary
            print(f"\n{'=' * 60}")
            print(f"Company: {response['company']}")
            print(f"Topic: {args.topic}")
            print(f"Depth: {args.depth}")
            print(f"Events: {len(response['events'])}")
            print(f"Signals: {len(response['signals'])}")
            print(
                f"Cost: ${response['cost']['total_cost']:.6f} / ${response['budget']['requested']:.2f}"
            )
            print(f"Usage: {response['usage']['total_tokens']} tokens")
            print(f"{'=' * 60}")

            if response["events"]:
                print("\n Events:")
                for e in response["events"]:
                    print(f"  - [{e['date']}] {e['headline'][:70]}")
                    print(f"    {e['source_name']} | {e['content_type']}")

            if response["signals"]:
                print("\n Signals:")
                for s in response["signals"]:
                    print(f"  - [{s['signal_type']}] confidence={s['confidence']}")
                    print(f"    {s['headline'][:70]}")

            if response["answers"]:
                print("\n Answers:")
                for a in response["answers"]:
                    valid = "[valid]  " if a["validity"]["is_valid"] else "[invalid]"
                    print(f"  {valid} {a['topic']}: {a['summary'][:100]}")

    finally:
        await scrape.close()
        await search.close()
        await llm.close()


def main() -> None:
    """Synchronous entry point for asyncio.run()."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
