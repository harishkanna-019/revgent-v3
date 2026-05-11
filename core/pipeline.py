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

        # ── Resolve company names once (used by stop protocol) ──
        company_names, _ = await company.get_names(ctx.company)

        # ── Process each topic ──
        for topic_name in ctx.topics:
            if ctx.exhausted:
                break

            ctx.topic = TopicState(original=topic_name)

            # ═══════════════════════════════════════════════
            # Stage 1: Topic analysis
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="topic_analysis", count=1))

            if ctx.policy.depth == "cheap":
                # Cheap: regex keywords from topic words
                keywords = [w.lower() for w in topic_name.split() if len(w) > 2]
                ctx.topic.simplified = topic_name.strip().lower()
                ctx.topic.keywords = keywords
            else:
                # Standard/Deep: LLM-based topic analysis
                topic_result = await topic.analyze(ctx)
                ctx.topic.simplified = topic_result.output.get(
                    "simplified", topic_name.strip().lower()
                )
                ctx.topic.keywords = topic_result.output.get("keywords", [])
                # Cost already recorded by topic.analyze()

            _emit(StageEnd(stage="topic_analysis", out=len(ctx.topic.keywords)))
            _emit_budget()

            if ctx.exhausted:
                break

            # ═══════════════════════════════════════════════
            # Stage 2: Query generation
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="query_generation", count=1))

            if ctx.policy.depth == "cheap":
                # Cheap: 2 hardcoded queries
                company_stem = (
                    ctx.company.strip()
                    .lower()
                    .replace("www.", "")
                    .split("://")[-1]
                    .split("/")[0]
                )
                simplified = ctx.topic.simplified
                ctx.topic.queries = [
                    f"{company_stem} {simplified}",
                    f"{company_stem} {simplified} news",
                ]
            else:
                # Standard/Deep: LLM-generated queries
                queries_result = await queries.generate(ctx)
                ctx.topic.queries = queries_result.output
                # Cost already recorded by queries.generate()

            _emit(StageEnd(stage="query_generation", out=len(ctx.topic.queries)))
            _emit_budget()

            if ctx.exhausted or not ctx.topic.queries:
                break

            # ═══════════════════════════════════════════════
            # Stage 3: Search
            # ═══════════════════════════════════════════════
            _emit(StageStart(stage="search", count=len(ctx.topic.queries)))

            from providers import search as search_provider

            search_results = await search_provider.search_many(
                ctx.topic.queries,
                max_days=ctx.date_max,
                limit=10,
            )

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

                # Route via classify_result
                decision = classify_result(
                    candidate, is_valid, is_hard_fact, fact_check_raw, topic_name
                )

                # For standard/deep, replace with LLM-formatted event
                if ctx.policy.depth != "cheap" and decision.lane == "event":
                    from tools import format as fmt

                    fmt_result = await fmt.format_one(ctx, candidate)
                    decision = LaneDecision(
                        lane="event",
                        event=fmt_result.output,
                        signal=None,
                    )

                if decision.lane == "event" and decision.event is not None:
                    ctx.events.append(decision.event)
                elif decision.lane == "signal" and decision.signal is not None:
                    ctx.signals.append(decision.signal)
                # discard: do nothing

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
