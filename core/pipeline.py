"""Research pipeline — one async function that assembles all components.

One function for all depths, with conditional branches for cheap/standard/deep.
Budget enforcement between stages. Event emission via callback.
Optional timeout via asyncio.wait_for.
"""

import asyncio

from core.context import RunContext, TopicState
from core.runner import parallel
from core.types import BudgetCheck, Emit, ItemResult, StageEnd, StageStart, ToolResult
from filters.dedup import dedup_urls
from filters.ranker import rank
from filters.signals import LaneDecision, classify_result
from filters.stop_protocol import apply_stop_protocol
from tools import company, queries, topic, validate

# Synonym expansion for cheap-depth keyword filtering. Cheap depth runs
# zero LLM calls before search, so we have to expand topic terms manually
# or stop_protocol's literal keyword match drops valid headlines that
# describe the same event with different vocabulary ("layoffs" vs.
# "job cuts" vs. "workforce reduction"). Standard / deep paths use
# tools.topic which does this expansion via the LLM.
_CHEAP_SYNONYMS: dict[str, tuple[str, ...]] = {
    "layoffs": (
        "layoff",
        "layoffs",
        "laid off",
        "laying off",
        "job cut",
        "job cuts",
        "jobs cut",
        "cut jobs",
        "cutting jobs",
        "cuts jobs",
        "cuts hundreds of jobs",
        "cuts thousands of jobs",
        "axes jobs",
        "axe jobs",
        "axed jobs",
        "slash jobs",
        "slashed jobs",
        "slashes jobs",
        "eliminate positions",
        "eliminated positions",
        "eliminating positions",
        "cut positions",
        "position cuts",
        "role cuts",
        "cuts roles",
        "cut roles",
        "headcount",
        "head count",
        "head-count",
        "workforce",
        "workforce reduction",
        "workforce cut",
        "workforce cuts",
        "staff cut",
        "staff cuts",
        "reduce staff",
        "reducing staff",
        "reduce workforce",
        "reducing workforce",
        "reduction in force",
        "rif",
        "redundancies",
        "redundancy",
        "firing",
        "fired",
        "firings",
        "mass firing",
        "mass firings",
        "restructuring",
        "restructure",
        "restructures",
        "reorganization",
        "reorganisation",
        "reorganize",
        "reorganise",
        "downsizing",
        "downsize",
        "downsized",
        "trim costs",
        "trimming costs",
        "cost cutting",
        "cost-cutting",
        "cost cuts",
        "cost savings",
        "cost-savings",
        "belt tightening",
        "belt-tightening",
        "hiring freeze",
        "hiring pause",
        "rightsizing",
        "right-sizing",
    ),
    "funding": (
        "funding",
        "funded",
        "raised",
        "raises",
        "raise",
        "series a",
        "series b",
        "series c",
        "series d",
        "seed round",
        "venture",
        "investment",
        "invested",
        "investor",
        "valuation",
        "valued at",
        "capital",
    ),
    "earnings": (
        "earnings",
        "revenue",
        "profit",
        "loss",
        "q1",
        "q2",
        "q3",
        "q4",
        "quarterly",
        "quarter",
        "financial results",
        "fiscal",
        "ebitda",
    ),
    "acquisition": (
        "acquired",
        "acquires",
        "acquisition",
        "buyout",
        "bought",
        "merger",
        "merging",
        "merge",
        "takeover",
        "deal",
    ),
    "product launch": (
        "launch",
        "launches",
        "launched",
        "announces",
        "unveils",
        "reveals",
        "introduces",
        "releases",
        "new product",
    ),
    "leadership": (
        "ceo",
        "cto",
        "cfo",
        "founder",
        "chief executive",
        "president",
        "appointed",
        "appoints",
        "steps down",
        "resigns",
        "resignation",
        "departure",
        "hires",
        "hired",
        "executive",
    ),
}


def _expand_topic_keywords(topic_name: str) -> list[str]:
    """Expand a topic name into keyword variants for stop-protocol filtering.

    Looks up the lowercase topic in _CHEAP_SYNONYMS; falls back to the raw
    words (>=3 chars) of the topic when the topic is unknown. Always
    includes the original topic words so familiar phrasings still match.
    """
    raw = topic_name.strip().lower()
    raw_words = [w for w in raw.split() if len(w) > 2]

    # Direct lookup
    if raw in _CHEAP_SYNONYMS:
        return [raw, *_CHEAP_SYNONYMS[raw]]

    # Token-level lookup (e.g. "workforce reduction" -> tries 'reduction')
    for token in raw_words:
        if token in _CHEAP_SYNONYMS:
            return [raw, *raw_words, *_CHEAP_SYNONYMS[token]]

    # Fallback: just the raw words
    return raw_words or [raw]


async def run(
    ctx: RunContext, emit: Emit = None, timeout_seconds: float | None = None
) -> dict:
    """Run the full research pipeline.

    Flow per topic:
    1. Topic analysis (cheap: regex keywords; standard/deep: LLM)
    2. Query generation (cheap: hardcoded; standard/deep: LLM)
    3. Search (concurrent via search_many)
    4. Deduplicate URLs
    5. Stop protocol filter (date, source, topic, company relevance)
    6. Rank candidates
    7. Scrape top N (standard/deep only, limited by max_full_extraction_candidates)
    8. Validate top N via LLM (parallel)
    9. Format valid results (cheap: snippet; standard/deep: LLM summary)
    10. Signal routing (event vs signal vs discard)
    11. Cost attribution

    Budget checks between stages. Exhaustion produces partial response.
    Timeout cancels in-flight work and returns partial response.

    Args:
        ctx: Per-request mutable state with policy, company, topics, budget
        emit: Optional callback receiving StageStart, StageEnd, ItemResult,
              BudgetCheck events
        timeout_seconds: Optional wall-clock timeout. If exceeded, pipeline
            is cancelled and returns whatever was accumulated so far.

    Returns:
        v2-identical ResearchResponse dict (may be partial on timeout)
    """

    async def _pipeline() -> dict:
        def _emit(event: BudgetCheck | StageStart | StageEnd | ItemResult) -> None:
            if emit is not None:
                emit(event)

        def _emit_budget() -> None:
            _emit(
                BudgetCheck(
                    spent=round(ctx.cost.total_cost, 8),
                    remaining=round(max(0.0, ctx.cost.budget - ctx.cost.total_cost), 8),
                )
            )

        # ── Resolve company names + first topic in parallel ──
        # company.get_names() is independent of topic analysis and query
        # generation. For the first topic, run all three concurrently to
        # save one LLM round-trip (~2-3s). Subsequent topics (if any)
        # only need topic analysis + query generation since company
        # names are cached.
        company_names_task = asyncio.create_task(
            company.get_names(
                ctx.company,
                model=ctx.policy.model_for_task("keyword_generation"),
            )
        )

        # ── Process each topic ──
        for topic_idx, topic_name in enumerate(ctx.topics):
            if ctx.exhausted:
                break

            ctx.topic = TopicState(original=topic_name)

            # ═══════════════════════════════════════════════
            # Stage 1+2: Topic analysis + Query generation
            # ═══════════════════════════════════════════════
            # Run topic analysis and query generation concurrently.
            # For the first topic, also await the company names task.
            _emit(StageStart(stage="topic_analysis", count=1))
            _emit(StageStart(stage="query_generation", count=1))

            if ctx.policy.depth == "cheap":
                ctx.topic.simplified = topic_name.strip().lower()
                ctx.topic.keywords = _expand_topic_keywords(topic_name)

                _emit(StageEnd(stage="topic_analysis", out=len(ctx.topic.keywords)))
                _emit_budget()

                if ctx.exhausted:
                    break

                # Await company names (first topic only; cached after).
                if not ctx.company_names:
                    company_names, _ = await company_names_task
                    ctx.company_names = company_names
                company_names = ctx.company_names

                # Cheap depth: zero-LLM query generation.
                # Cheap depth: zero-LLM query generation. Use the canonical
                # company names resolved earlier (these include
                # human-readable variants like "group 1 automotive" or
                # "clearwater paper" instead of the raw URL stem). Without
                # this, queries like "group1auto layoffs" miss every
                # relevant article because real journalists write about
                # "Group 1 Automotive".
                simplified = ctx.topic.simplified
                # Skip the bare URL stem (always the first element of
                # company_names) when it contains no space and a longer
                # spaced variant exists.
                preferred = [n for n in company_names if " " in n] or company_names[:1]
                # Fall back to the URL stem if nothing else is available.
                if not preferred:
                    preferred = [
                        ctx.company.strip()
                        .lower()
                        .replace("www.", "")
                        .split("://")[-1]
                        .split("/")[0]
                    ]
                # Two queries per preferred name (cap at policy max).
                generated: list[str] = []
                for name in preferred:
                    generated.append(f"{name} {simplified}")
                    generated.append(f"{name} {simplified} news")
                # Dedupe and clip to policy budget.
                seen: set[str] = set()
                deduped: list[str] = []
                for q in generated:
                    key = q.lower()
                    if key not in seen:
                        seen.add(key)
                        deduped.append(q)
                ctx.topic.queries = deduped[: ctx.policy.max_queries_per_topic or 2]
            else:
                # Standard/Deep: run topic analysis (keywords) and query
                # generation concurrently. Both use the simplified topic
                # name (set eagerly above for cheap; for standard/deep
                # short topics <=3 words are pre-set, long topics go
                # through simplification first which is rare).
                # Set simplified eagerly so queries.generate can use it.
                words = topic_name.strip().split()
                if len(words) <= 3:
                    ctx.topic.simplified = topic_name.strip().lower()

                topic_task = asyncio.create_task(topic.analyze(ctx))
                queries_task = asyncio.create_task(queries.generate(ctx))

                topic_result, queries_result = await asyncio.gather(
                    topic_task, queries_task
                )

                ctx.topic.simplified = topic_result.output.get(
                    "simplified", topic_name.strip().lower()
                )
                ctx.topic.keywords = topic_result.output.get("keywords", [])
                ctx.topic.queries = queries_result.output

                _emit(StageEnd(stage="topic_analysis", out=len(ctx.topic.keywords)))

            _emit(StageEnd(stage="query_generation", out=len(ctx.topic.queries)))
            _emit_budget()

            if ctx.exhausted or not ctx.topic.queries:
                break

            # Ensure company names are resolved before search/stop_protocol.
            # On the first topic this awaits the concurrent task started
            # before the loop. On subsequent topics it's a no-op (already done).
            if not ctx.company_names:
                company_names, _ = await company_names_task
                ctx.company_names = company_names
            company_names = ctx.company_names

            # ═══════════════════════════════════════════════
            # Stage 3: Search
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="search", count=len(ctx.topic.queries)))

            from providers import search as search_provider
            from providers.search import SearchCircuitOpen

            try:
                search_results = await search_provider.search_many(
                    ctx.topic.queries,
                    max_days=ctx.date_max,
                    limit=10,
                )
            except SearchCircuitOpen:
                # Circuit breaker is open — SearXNG is temporarily
                # unavailable. Return partial response with 0 results
                # instead of crashing the entire request with a 500.
                search_results = []

            _emit(StageEnd(stage="search", out=len(search_results)))
            _emit_budget()

            if ctx.exhausted or not search_results:
                continue

            # ═══════════════════════════════════════════════
            # Stage 4: Deduplicate
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="dedup", count=len(search_results)))
            deduped = dedup_urls(search_results)
            _emit(StageEnd(stage="dedup", out=len(deduped)))

            # ═══════════════════════════════════════════════
            # Stage 5: Stop protocol
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="stop_protocol", count=len(deduped)))
            filtered = apply_stop_protocol(
                deduped,
                topic=ctx.topic.simplified,
                company_names=company_names,
                min_days=ctx.date_min,
                max_days=ctx.date_max,
                topic_keywords=ctx.topic.keywords,
                strict_date=ctx.strict_date,
                skip_company_check=True,
            )
            _emit(StageEnd(stage="stop_protocol", out=len(filtered)))

            if not filtered:
                continue

            # ═══════════════════════════════════════════════
            # Stage 6: Rank
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="rank", count=len(filtered)))
            ranked = rank(filtered, ctx.topic.keywords)
            _emit(StageEnd(stage="rank", out=len(ranked)))

            # Take top N candidates
            max_candidates = ctx.policy.max_candidates_per_topic
            candidates = ranked[:max_candidates]

            if not candidates:
                continue

            # ═══════════════════════════════════════════════
            # Stage 7: Scrape (standard/deep only, top N)
            # ═══════════════════════════════════════════════
            if ctx.policy.depth != "cheap" and ctx.policy.max_extraction_chars > 0:
                _emit(
                    StageStart(
                        stage="scrape",
                        count=min(
                            len(candidates), ctx.policy.max_full_extraction_candidates
                        ),
                    )
                )

                from providers import scrape

                # Only scrape top max_full_extraction_candidates
                scrape_candidates = candidates[
                    : ctx.policy.max_full_extraction_candidates
                ]
                urls = [c.get("url", "") for c in scrape_candidates if c.get("url")]
                if urls:
                    scraped = await scrape.scrape_many(urls)
                    for c in scrape_candidates:
                        url = c.get("url", "")
                        if url in scraped and scraped[url]:
                            c["content"] = scraped[url][
                                : ctx.policy.max_extraction_chars
                            ]

                _emit(StageEnd(stage="scrape", out=len(urls)))
                _emit_budget()

            if ctx.exhausted:
                break

            # ═══════════════════════════════════════════════
            # Stage 8: Validate (parallel)
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="validate", count=len(candidates)))

            async def _validate_one(candidate: dict) -> ToolResult | BaseException:
                try:
                    result = await validate.validate_one(ctx, candidate)
                    status = result.output["status"]
                    _emit(
                        ItemResult(
                            stage="validate",
                            item_id=result.item_id or candidate.get("url", ""),
                            status=status,
                        )
                    )
                    return result
                except Exception as exc:
                    _emit(
                        ItemResult(
                            stage="validate",
                            item_id=candidate.get("url", ""),
                            status=f"error: {type(exc).__name__}",
                        )
                    )
                    return exc

            validation_results = await parallel(
                _validate_one,
                candidates,
                max_workers=ctx.policy.max_workers,
            )

            valid_count = sum(
                1
                for r in validation_results
                if isinstance(r, ToolResult)
                and r.output["status"] in ("valid", "opinion")
            )
            _emit(StageEnd(stage="validate", out=valid_count))
            _emit_budget()

            if ctx.exhausted:
                break

            # ═══════════════════════════════════════════════
            # Stage 9: Format + Route
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="format_route", count=valid_count))

            # First pass: classify every validation result into a lane decision.
            # Collect event-lane candidates that need LLM formatting (standard/deep).
            pending: list[tuple[int, LaneDecision, dict]] = []
            to_format: list[dict] = []

            for val_result in validation_results:
                if isinstance(val_result, BaseException):
                    continue
                if not isinstance(val_result, ToolResult):
                    continue

                status = val_result.output["status"]
                if status == "not_about_company":
                    continue

                result = val_result.output["result"]
                if result is None:
                    continue

                is_valid = result["is_valid"]
                is_hard_fact = result["is_hard_fact"]
                fact_check_raw = result["fact_check_raw"]
                candidate = val_result.output["original"]

                decision = classify_result(
                    candidate, is_valid, is_hard_fact, fact_check_raw, topic_name
                )

                if ctx.policy.depth != "cheap" and decision.lane == "event":
                    # Defer LLM-formatting to a parallel batch below.
                    pending.append((len(to_format), decision, candidate))
                    to_format.append(candidate)
                else:
                    pending.append((-1, decision, candidate))

            # Cap event-lane formatting at 5 candidates. The answer_builder
            # only uses the top 2-3 events (by content_type rank + date), so
            # formatting more than 5 wastes LLM calls.
            max_format = 5
            if len(to_format) > max_format:
                to_format = to_format[:max_format]
                pending = [
                    (idx, dec, cand) if idx < max_format else (-1, dec, cand)
                    for idx, dec, cand in pending
                ]

            # Parallel LLM format pass for event-lane candidates (standard/deep only).
            formatted_events: list[dict | None] = [None] * len(to_format)
            if to_format:
                from tools import format as fmt

                async def _format_one(cand: dict) -> ToolResult | BaseException:
                    try:
                        return await fmt.format_one(ctx, cand)
                    except Exception as exc:
                        return exc

                format_results = await parallel(
                    _format_one,
                    to_format,
                    max_workers=ctx.policy.max_workers,
                )
                for i, fr in enumerate(format_results):
                    if isinstance(fr, ToolResult):
                        formatted_events[i] = fr.output

            # Second pass: apply formatted events and append in source order.
            for fmt_idx, decision, _candidate in pending:
                if fmt_idx >= 0:
                    formatted = formatted_events[fmt_idx]
                    if formatted is not None:
                        # Ensure topic is stamped (defence in depth - format_one
                        # should already have done this).
                        if not formatted.get("topic"):
                            formatted["topic"] = topic_name
                        decision = LaneDecision(
                            lane="event",
                            event=formatted,
                            signal=None,
                        )
                    # If formatting failed (None), fall back to the raw decision
                    # which still has decision.event from classify_result.

                if decision.lane == "event" and decision.event is not None:
                    # Final safety: ensure every appended event has a topic.
                    if not decision.event.get("topic"):
                        decision.event["topic"] = topic_name
                    ctx.events.append(decision.event)
                elif decision.lane == "signal" and decision.signal is not None:
                    ctx.signals.append(decision.signal)
                # discard: do nothing

            # Stamp company_name on all events so Clay can match events
            # to the right company. Use longest multi-word variant for specificity.
            if ctx.company_names:
                preferred_name = [n for n in ctx.company_names if " " in n] or ctx.company_names[:1]
            else:
                preferred_name = [ctx.company]
            for e in ctx.events:
                e["company_name"] = preferred_name[0]
            for s in ctx.signals:
                s["company_name"] = preferred_name[0]

            _emit(StageEnd(stage="format_route", out=len(ctx.events)))
            _emit_budget()

        # ═══════════════════════════════════════════════
        # Final: Cost attribution
        # ═══════════════════════════════════════════════
        _emit(StageStart(stage="cost_attribution", count=1))

        # Collect all item IDs (events + signals) and amortize shared costs once
        all_item_ids: list[str] = []
        for i, e in enumerate(ctx.events):
            all_item_ids.append(e.get("source_url", f"event-{i}"))
        for i, s in enumerate(ctx.signals):
            all_item_ids.append(s.get("source_url", f"signal-{i}"))

        if all_item_ids:
            amortized = ctx.cost.amortize_shared(all_item_ids)
            for i, e in enumerate(ctx.events):
                item_id = e.get("source_url", f"event-{i}")
                e["cost_attribution"] = round(
                    ctx.cost.cost_for_item(item_id, amortized), 8
                )
            for i, s in enumerate(ctx.signals):
                item_id = s.get("source_url", f"signal-{i}")
                s["cost_attribution"] = round(
                    ctx.cost.cost_for_item(item_id, amortized), 8
                )

        _emit(StageEnd(stage="cost_attribution", out=1))

        # ═══════════════════════════════════════════════
        # Final: Build response
        # ═══════════════════════════════════════════════
        _emit(StageStart(stage="build_response", count=1))
        response = ctx.build_response(topic_name=ctx.topics[-1] if ctx.topics else "")
        _emit(StageEnd(stage="build_response", out=1))

        return response

    # ── Timeout wrapper ──
    if timeout_seconds is not None:
        try:
            return await asyncio.wait_for(_pipeline(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            # Return partial response with whatever was accumulated
            return ctx.build_response(topic_name=ctx.topics[-1] if ctx.topics else "")
    else:
        return await _pipeline()
