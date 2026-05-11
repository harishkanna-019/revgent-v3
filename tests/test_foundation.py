"""Foundation layer tests — pure logic, no infrastructure, no mocks."""

import asyncio
import time

import pytest

from answer_builder import build_answers
from cache import AsyncTTLCache
from core.context import RunContext, TopicState
from core.depth import ResearchDepthPolicy
from core.runner import parallel
from core.types import BudgetCheck, ItemResult, StageEnd, StageStart, ToolResult
from formatting import (
    extract_date_from_content,
    format_event,
    headline_has_numbers,
    parse_date,
)
from models import CostTracker, UsageStats


# ───────────────────────────────
# core/types.py
# ───────────────────────────────

class TestToolResult:
    def test_frozen(self):
        tr = ToolResult(output="hello", usage={"input_tokens": 10})
        with pytest.raises(FrozenInstanceError):
            tr.output = "world"

    def test_default_usage(self):
        tr = ToolResult(output="hello")
        assert tr.usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def test_fields(self):
        tr = ToolResult(output={"simplified": "layoffs", "keywords": ["jobs"]}, usage={"input_tokens": 5}, item_id="item-1")
        assert tr.output["simplified"] == "layoffs"
        assert tr.item_id == "item-1"


class TestEventTypes:
    def test_stage_start(self):
        ev = StageStart(stage="search", count=5)
        assert ev.type == "stage_start"
        assert ev.stage == "search"
        assert ev.count == 5

    def test_stage_end(self):
        ev = StageEnd(stage="search", out=3)
        assert ev.type == "stage_end"
        assert ev.out == 3

    def test_item_result(self):
        ev = ItemResult(stage="validate", item_id="url-1", status="ok")
        assert ev.type == "item_result"
        assert ev.status == "ok"

    def test_budget_check(self):
        ev = BudgetCheck(spent=0.12, remaining=0.38)
        assert ev.type == "budget"
        assert ev.spent == 0.12

    def test_all_frozen(self):
        for cls in [StageStart, StageEnd, ItemResult, BudgetCheck]:
            obj = cls()
            with pytest.raises(FrozenInstanceError):
                obj.type = "x"


# ───────────────────────────────
# core/depth.py
# ───────────────────────────────

class TestResearchDepthPolicy:
    def test_cheap_profile(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        assert policy.depth == "cheap"
        assert policy.max_candidates_per_topic == 3
        assert policy.max_queries_per_topic == 2
        assert policy.default_budget == 0.01
        assert policy.max_workers == 3
        assert policy.max_extraction_chars == 0
        assert policy.max_full_extraction_candidates == 0

    def test_standard_profile(self):
        policy = ResearchDepthPolicy.from_request("standard")
        assert policy.depth == "standard"
        assert policy.max_candidates_per_topic == 10
        assert policy.max_queries_per_topic == 8
        assert policy.default_budget == 0.50
        assert policy.max_workers == 8
        assert policy.max_extraction_chars == 4000
        assert policy.max_full_extraction_candidates == 5

    def test_deep_profile(self):
        policy = ResearchDepthPolicy.from_request("deep")
        assert policy.depth == "deep"
        assert policy.max_candidates_per_topic == 20
        assert policy.max_queries_per_topic == 12
        assert policy.default_budget == 2.00
        assert policy.max_workers == 16
        assert policy.max_extraction_chars == 8000
        assert policy.max_full_extraction_candidates == 20

    def test_frozen(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        with pytest.raises(FrozenInstanceError):
            policy.depth = "deep"

    def test_model_for_task_cheap(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        assert policy.model_for_task("validation") == "deepseek/deepseek-v4-flash:nitro"
        assert policy.model_for_task("summarization") == "deepseek/deepseek-v4-flash:nitro"

    def test_model_for_task_standard(self):
        policy = ResearchDepthPolicy.from_request("standard")
        assert policy.model_for_task("validation") == "deepseek/deepseek-v4-flash:nitro"
        assert policy.model_for_task("classification") == "deepseek/deepseek-v4-flash:nitro"

    def test_model_for_task_deep(self):
        policy = ResearchDepthPolicy.from_request("deep")
        assert policy.model_for_task("validation") == "moonshotai/kimi-k2.6:nitro"
        assert policy.model_for_task("summarization") == "moonshotai/kimi-k2.6:nitro"
        assert policy.model_for_task("query_generation") == "deepseek/deepseek-v4-pro:nitro"

    def test_model_for_task_default_fallback(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        assert policy.model_for_task("unknown_task") == "deepseek/deepseek-v4-flash:nitro"

    def test_budget_cap(self):
        policy = ResearchDepthPolicy.from_request("cheap", max_cost=10.0)
        assert policy.default_budget == 5.0  # user max_cost capped at absolute max of 5.0

    def test_unknown_depth_defaults_to_standard(self):
        policy = ResearchDepthPolicy.from_request("garbage")
        assert policy.depth == "garbage"  # passes through, but uses standard profile
        assert policy.max_candidates_per_topic == 10


# ───────────────────────────────
# models.py
# ───────────────────────────────

class TestUsageStats:
    def test_add(self):
        u = UsageStats()
        u.add({"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        assert u.input_tokens == 100
        assert u.output_tokens == 50
        assert u.total_tokens == 150

    def test_add_multiple(self):
        u = UsageStats()
        u.add({"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        u.add({"input_tokens": 20, "output_tokens": 10, "total_tokens": 30})
        assert u.input_tokens == 30
        assert u.total_tokens == 45

    def test_to_dict(self):
        u = UsageStats(input_tokens=100, output_tokens=50, total_tokens=150)
        assert u.to_dict() == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}


class TestCostTracker:
    def test_record_direct(self):
        ct = CostTracker(budget=1.0)
        ct.record(0.10, item_id="item-1")
        assert ct.total_cost == 0.10
        assert ct.per_item["item-1"] == 0.10
        assert ct.breakdown["llm"] == 0.10

    def test_record_shared(self):
        ct = CostTracker(budget=1.0)
        ct.record(0.30)  # shared, no item_id
        assert ct.total_cost == 0.30
        assert ct._shared_pending == [(0.30, 1)]

    def test_amortize_shared(self):
        ct = CostTracker(budget=1.0)
        ct.record(0.30)  # shared
        amortized = ct.amortize_shared(["a", "b", "c"])
        assert round(amortized["a"], 10) == 0.10
        assert round(amortized["b"], 10) == 0.10
        assert round(amortized["c"], 10) == 0.10
        assert not ct._shared_pending

    def test_cost_for_item(self):
        ct = CostTracker(budget=1.0)
        ct.record(0.20, item_id="item-1")
        amortized = ct.amortize_shared(["item-1", "item-2"])
        # item-1: 0.20 direct + 0.0 shared (no shared pending)
        assert ct.cost_for_item("item-1") == 0.20

    def test_is_exhausted(self):
        ct = CostTracker(budget=1.0)
        assert not ct.is_exhausted
        ct.record(1.0)
        assert ct.is_exhausted
        ct.record(0.1)
        assert ct.is_exhausted  # over budget

    def test_to_dict(self):
        ct = CostTracker(budget=0.50, total_cost=0.123456789)
        d = ct.to_dict()
        assert d["total_cost"] == round(0.123456789, 8)
        assert d["budget"] == 0.50
        assert d["budget_exhausted"] is False
        assert "llm" in d["breakdown"]


# ───────────────────────────────
# cache.py
# ───────────────────────────────

class TestAsyncTTLCache:
    @pytest.mark.asyncio
    async def test_get_set(self):
        cache = AsyncTTLCache(ttl_seconds=10)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    @pytest.mark.asyncio
    async def test_get_missing(self):
        cache = AsyncTTLCache(ttl_seconds=10)
        assert cache.get("missing") is None

    @pytest.mark.asyncio
    async def test_ttl_expiration(self):
        cache = AsyncTTLCache(ttl_seconds=0.1)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"
        await asyncio.sleep(0.15)
        assert cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_get_or_compute_basic(self):
        cache = AsyncTTLCache(ttl_seconds=10)
        call_count = 0

        async def compute():
            nonlocal call_count
            call_count += 1
            return "computed"

        result = await cache.get_or_compute("key1", compute)
        assert result == "computed"
        assert call_count == 1

        # Second call should hit cache
        result2 = await cache.get_or_compute("key1", compute)
        assert result2 == "computed"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_get_or_compute_thundering_herd(self):
        """100 concurrent misses on the same key → 1 compute, 99 wait."""
        cache = AsyncTTLCache(ttl_seconds=10)
        compute_count = 0
        compute_delay = 0.05

        async def compute():
            nonlocal compute_count
            compute_count += 1
            await asyncio.sleep(compute_delay)
            return f"result-{compute_count}"

        async def fetch():
            return await cache.get_or_compute("herd-key", compute)

        # Launch 100 concurrent get_or_compute calls
        tasks = [asyncio.create_task(fetch()) for _ in range(100)]
        results = await asyncio.gather(*tasks)

        assert compute_count == 1, f"Expected 1 compute, got {compute_count}"
        assert all(r == "result-1" for r in results)

    @pytest.mark.asyncio
    async def test_invalidate(self):
        cache = AsyncTTLCache(ttl_seconds=10)
        cache.set("key1", "value1")
        cache.invalidate("key1")
        assert cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_clear(self):
        cache = AsyncTTLCache(ttl_seconds=10)
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.clear()
        assert cache.get("key1") is None
        assert cache.get("key2") is None


# ───────────────────────────────
# core/runner.py
# ───────────────────────────────

class TestParallel:
    @pytest.mark.asyncio
    async def test_basic(self):
        async def double(x):
            return x * 2

        results = await parallel(double, [1, 2, 3, 4], max_workers=2)
        assert results == [2, 4, 6, 8]

    @pytest.mark.asyncio
    async def test_source_order(self):
        """Results returned in input order, not completion order."""
        delays = [0.1, 0.01, 0.05]

        async def delayed(idx):
            await asyncio.sleep(delays[idx])
            return idx

        results = await parallel(delayed, [0, 1, 2], max_workers=3)
        assert results == [0, 1, 2]

    @pytest.mark.asyncio
    async def test_exception_as_value(self):
        async def fail_on_even(x):
            if x % 2 == 0:
                raise ValueError(f"even: {x}")
            return x

        results = await parallel(fail_on_even, [0, 1, 2, 3], max_workers=4)
        assert isinstance(results[0], ValueError)
        assert results[1] == 1
        assert isinstance(results[2], ValueError)
        assert results[3] == 3

    @pytest.mark.asyncio
    async def test_empty_items(self):
        results = await parallel(lambda x: x, [], max_workers=2)
        assert results == []

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        """Verify that max_workers actually limits concurrency."""
        max_concurrent = 0
        current_concurrent = 0

        async def track(x):
            nonlocal max_concurrent, current_concurrent
            current_concurrent += 1
            max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.05)
            current_concurrent -= 1
            return x

        await parallel(track, [1, 2, 3, 4, 5], max_workers=2)
        assert max_concurrent == 2


# ───────────────────────────────
# core/context.py
# ───────────────────────────────

class TestRunContext:
    def test_slots(self):
        ctx = RunContext(
            policy=ResearchDepthPolicy.from_request("cheap"),
            company="meta.com",
            topics=["layoffs"],
            date_min=0,
            date_max=90,
        )
        # Verify __slots__ works — can't add arbitrary attributes
        with pytest.raises(AttributeError):
            ctx.arbitrary = "value"

    def test_exhausted(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        ctx = RunContext(
            policy=policy,
            company="meta.com",
            topics=["layoffs"],
            date_min=0,
            date_max=90,
        )
        assert not ctx.exhausted
        ctx.cost.record(0.01)  # exact budget for cheap
        assert ctx.exhausted

    def test_record(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        ctx = RunContext(
            policy=policy,
            company="meta.com",
            topics=["layoffs"],
            date_min=0,
            date_max=90,
        )
        ctx.record({"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}, item_id="url-1")
        assert ctx.usage.total_tokens == 1500
        assert "url-1" in ctx.cost.per_item
        assert ctx.cost.total_cost > 0

    def test_build_response_shape(self):
        policy = ResearchDepthPolicy.from_request("cheap")
        ctx = RunContext(
            policy=policy,
            company="meta.com",
            topics=["layoffs"],
            date_min=0,
            date_max=90,
        )
        ctx.events.append({
            "headline": "Meta lays off 1000",
            "description": "...",
            "topic": "layoffs",
            "date": "2026-01-16",
            "source_name": "reuters.com",
            "source_url": "https://reuters.com/...",
            "content_type": "novel_fact",
            "headline_has_numbers": True,
            "cost_attribution": 0.001,
        })
        resp = ctx.build_response("layoffs")
        assert resp["company"] == "meta.com"
        assert "events" in resp
        assert "answers" in resp
        assert "signals" in resp
        assert "usage" in resp
        assert "topic_results" in resp
        assert "cost" in resp
        assert "budget" in resp
        assert resp["cost"]["budget_exhausted"] is False
        assert resp["budget"]["requested"] == 0.01

    def test_topic_state(self):
        ts = TopicState(original="recent layoffs at meta")
        assert ts.simplified == ""
        ts.simplified = "layoffs"
        assert ts.simplified == "layoffs"


# ───────────────────────────────
# formatting.py
# ───────────────────────────────

class TestParseDate:
    def test_iso(self):
        assert parse_date("2026-01-16") == "2026-01-16"
        assert parse_date("2026-01-16T10:30:00Z") == "2026-01-16"

    def test_days_ago(self):
        result = parse_date("3 days ago")
        from datetime import datetime, timedelta
        expected = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        assert result == expected

    def test_hours_ago(self):
        result = parse_date("5 hours ago")
        from datetime import datetime
        expected = datetime.now().strftime("%Y-%m-%d")
        assert result == expected

    def test_dd_mm_yyyy(self):
        assert parse_date("16/01/2026") == "2026-01-16"

    def test_yyyy_mm_dd(self):
        assert parse_date("2026-01-16") == "2026-01-16"

    def test_unknown(self):
        assert parse_date("") == "Unknown"
        assert parse_date("Unknown") == "Unknown"
        assert parse_date("not a date") == "Unknown"


class TestFormatEvent:
    def test_basic(self):
        result = {
            "title": "Meta Layoffs",
            "url": "https://reuters.com/article",
            "content": "Meta announced layoffs...",
            "published_date": "2026-01-16",
        }
        event = format_event(result)
        assert event["headline"] == "Meta Layoffs"
        assert event["source_name"] == "reuters.com"
        assert event["source_url"] == "https://reuters.com/article"
        assert event["date"] == "2026-01-16"
        assert event["content_type"] == "analysis"
        assert event["headline_has_numbers"] is False

    def test_with_summary(self):
        result = {
            "title": "Meta Layoffs",
            "url": "https://reuters.com/article",
            "content": "Meta announced layoffs...",
            "published_date": "2026-01-16",
        }
        event = format_event(result, summary="AI summary here", content_type="novel_fact")
        assert event["description"] == "AI summary here"
        assert event["content_type"] == "novel_fact"

    def test_no_url(self):
        result = {"title": "Test", "url": "", "content": "...", "published_date": ""}
        event = format_event(result)
        assert event["source_name"] == ""
        assert event["date"] == "Unknown"


class TestHeadlineHasNumbers:
    def test_has_numbers(self):
        assert headline_has_numbers("Meta lays off 1,500 employees") is True
        assert headline_has_numbers("Revenue up 23%") is True

    def test_no_numbers(self):
        assert headline_has_numbers("Meta announces new policy") is False
        assert headline_has_numbers("") is False


class TestExtractDateFromContent:
    def test_iso_in_content(self):
        assert extract_date_from_content("On 2026-01-16, Meta announced...") == "2026-01-16"

    def test_no_date(self):
        assert extract_date_from_content("No dates here") == "Unknown"


# ───────────────────────────────
# answer_builder.py
# ───────────────────────────────

class TestBuildAnswers:
    def test_empty_events(self):
        answers = build_answers([], ["layoffs"])
        assert len(answers) == 1
        assert answers[0]["topic"] == "layoffs"
        assert answers[0]["validity"]["is_valid"] is False

    def test_single_topic(self):
        events = [
            {
                "headline": "Meta Layoffs",
                "description": "1000 people laid off",
                "topic": "layoffs",
                "date": "2026-01-16",
                "source_name": "reuters.com",
                "source_url": "https://reuters.com/...",
            }
        ]
        answers = build_answers(events, ["layoffs"])
        assert len(answers) == 1
        assert answers[0]["topic"] == "layoffs"
        assert answers[0]["validity"]["is_valid"] is True
        assert answers[0]["summary"] == "1000 people laid off"
        assert len(answers[0]["valid_sources"]) == 1

    def test_multiple_topics(self):
        events = [
            {"headline": "E1", "description": "D1", "topic": "layoffs", "date": "2026-01-16", "source_name": "s1", "source_url": "u1"},
            {"headline": "E2", "description": "D2", "topic": "earnings", "date": "2026-01-15", "source_name": "s2", "source_url": "u2"},
        ]
        answers = build_answers(events, ["layoffs", "earnings"])
        assert len(answers) == 2
        assert answers[0]["topic"] == "layoffs"
        assert answers[1]["topic"] == "earnings"

    def test_response_shape(self):
        """Verify the answer dict has all required v2 fields."""
        events = [{"headline": "H", "description": "D", "topic": "t", "date": "d", "source_name": "s", "source_url": "u"}]
        answers = build_answers(events, ["t"])
        a = answers[0]
        assert set(a.keys()) >= {"topic", "validity", "confirmation", "timing", "summary", "valid_sources"}
        assert set(a["validity"].keys()) >= {"is_valid", "statement", "confidence"}
        assert set(a["confirmation"].keys()) >= {"is_confirmed", "statement", "source_name", "source_url"}
        assert set(a["timing"].keys()) >= {"happened_at", "statement"}


# Ensure FrozenInstanceError is available for tests
from dataclasses import FrozenInstanceError
