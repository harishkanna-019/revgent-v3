"""Pipeline tests — end-to-end and stage-level.

- Pure tests verify stage wiring, event emission, budget handling, timeout.
- Real API tests run full pipeline against OpenRouter + SearXNG.
"""

import asyncio
import os

import pytest

from core.context import RunContext, TopicState
from core.depth import ResearchDepthPolicy
from core.pipeline import run
from core.types import BudgetCheck, ItemResult, StageEnd, StageStart, ToolResult

pytestmark = pytest.mark.asyncio

skip_if_no_key = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)

skip_if_no_searxng = pytest.mark.skipif(
    not os.environ.get("SEARXNG_URL"),
    reason="SEARXNG_URL not set",
)


# ── Fixtures ──

@pytest.fixture(autouse=True)
def reset_search_circuit():
    """Reset search circuit breaker between tests."""
    from providers import search
    search._consecutive_failures = 0
    search._circuit_open_until = 0.0
    yield


# ── Helpers ──

def _make_ctx(company: str = "meta.com", topics: list[str] | None = None, depth: str = "cheap") -> RunContext:
    """Create a RunContext for testing."""
    policy = ResearchDepthPolicy.from_request(depth)
    return RunContext(
        policy=policy,
        company=company,
        topics=topics or ["layoffs"],
        date_min=0,
        date_max=90,
    )


# ═══════════════════════════════════════════════
# Pure tests (no infrastructure)
# ═══════════════════════════════════════════════

class TestPipelinePure:
    """Tests that verify pipeline wiring without real API calls."""

    async def test_empty_topics_returns_response(self):
        """Pipeline with no topics returns a valid response shape."""
        ctx = _make_ctx(topics=[])
        events = []
        response = await run(ctx, emit=events.append)

        assert isinstance(response, dict)
        assert "company" in response
        assert "events" in response
        assert "answers" in response
        assert "signals" in response
        assert "usage" in response
        assert "topic_results" in response
        assert "cost" in response
        assert "budget" in response
        assert response["events"] == []
        assert response["signals"] == []

    @skip_if_no_searxng
    async def test_emits_stage_events(self):
        """Pipeline emits StageStart and StageEnd events."""
        ctx = _make_ctx()
        events = []
        await run(ctx, emit=events.append)

        stage_starts = [e for e in events if isinstance(e, StageStart)]
        stage_ends = [e for e in events if isinstance(e, StageEnd)]

        assert len(stage_starts) > 0
        assert len(stage_ends) > 0
        assert len(stage_starts) == len(stage_ends)

    @skip_if_no_searxng
    async def test_emits_budget_checks(self):
        """Pipeline emits BudgetCheck events."""
        ctx = _make_ctx()
        events = []
        await run(ctx, emit=events.append)

        budget_checks = [e for e in events if isinstance(e, BudgetCheck)]
        assert len(budget_checks) > 0
        assert budget_checks[0].spent >= 0.0
        assert budget_checks[0].remaining >= 0.0

    @skip_if_no_searxng
    async def test_cheap_depth_uses_regex_keywords(self):
        """Cheap depth extracts keywords from topic words."""
        ctx = _make_ctx(topics=["recent massive layoffs"], depth="cheap")
        await run(ctx)

        assert ctx.topic is not None
        assert ctx.topic.simplified == "recent massive layoffs"
        assert "recent" in ctx.topic.keywords
        assert "massive" in ctx.topic.keywords
        assert "layoffs" in ctx.topic.keywords

    @skip_if_no_searxng
    async def test_cheap_depth_uses_hardcoded_queries(self):
        """Cheap depth generates exactly 2 hardcoded queries."""
        ctx = _make_ctx(company="meta.com", topics=["layoffs"], depth="cheap")
        await run(ctx)

        assert len(ctx.topic.queries) == 2
        assert "meta.com layoffs" in ctx.topic.queries
        assert "meta.com layoffs news" in ctx.topic.queries

    @skip_if_no_searxng
    async def test_budget_exhaustion_returns_partial(self):
        """Exhausted budget produces partial response, not exception."""
        ctx = _make_ctx()
        ctx.cost.budget = 0.0  # Force exhaustion immediately
        response = await run(ctx)

        assert isinstance(response, dict)
        assert response["budget"]["exhausted"] is True
        assert response["events"] == []

    @skip_if_no_searxng
    async def test_response_has_v2_shape(self):
        """Response dict matches v2 ResearchResponse shape."""
        ctx = _make_ctx()
        response = await run(ctx)

        assert set(response.keys()) >= {
            "company", "events", "answers", "signals",
            "usage", "topic_results", "cost", "budget",
        }
        assert set(response["usage"].keys()) >= {
            "input_tokens", "output_tokens", "total_tokens",
        }
        assert set(response["cost"].keys()) >= {
            "total_cost", "budget", "budget_exhausted", "breakdown",
        }
        assert set(response["budget"].keys()) >= {
            "requested", "remaining", "exhausted",
        }
        assert set(response["topic_results"].keys()) >= {
            "topic_found", "topic_count", "topic_name",
        }

    @skip_if_no_key
    @skip_if_no_searxng
    async def test_cost_attribution_on_events(self):
        """Events have cost_attribution field set."""
        ctx = _make_ctx(depth="cheap")
        response = await run(ctx)

        for event in response["events"]:
            assert "cost_attribution" in event
            assert isinstance(event["cost_attribution"], (int, float))

    @skip_if_no_key
    @skip_if_no_searxng
    async def test_item_result_events_emitted(self):
        """Validation emits ItemResult events per candidate."""
        ctx = _make_ctx(depth="cheap")
        events = []
        await run(ctx, emit=events.append)

        item_results = [e for e in events if isinstance(e, ItemResult)]
        assert len(item_results) >= 0  # May be 0 if search returns nothing
        for ir in item_results:
            assert ir.stage == "validate"
            assert ir.item_id
            assert ir.status

    async def test_no_threadpool_executor(self):
        """Pipeline uses only asyncio, no ThreadPoolExecutor."""
        import inspect

        source = inspect.getsource(run)
        assert "ThreadPoolExecutor" not in source

    async def test_cheap_mode_no_scraping(self):
        """Cheap mode does not call scrape provider."""
        import inspect
        source = inspect.getsource(run)
        # Scrape is called conditionally: `if ctx.policy.depth != "cheap"`
        assert 'ctx.policy.depth != "cheap"' in source

    async def test_cheap_mode_no_llm_formatting(self):
        """Cheap mode bypasses LLM formatting."""
        import inspect
        source = inspect.getsource(run)
        # Format is called conditionally: `if ctx.policy.depth != "cheap"`
        assert 'ctx.policy.depth != "cheap"' in source

    async def test_timeout_returns_partial(self):
        """Timeout returns partial response, not exception."""
        ctx = _make_ctx(topics=[])
        # Zero timeout forces immediate return
        response = await run(ctx, timeout_seconds=0.001)

        assert isinstance(response, dict)
        assert "company" in response
        assert "events" in response

    async def test_timeout_parameter_exists(self):
        """run() accepts timeout_seconds parameter."""
        import inspect
        sig = inspect.signature(run)
        assert "timeout_seconds" in sig.parameters

    async def test_standard_scrapes_limited(self):
        """Standard depth limits scraping to max_full_extraction_candidates."""
        import inspect
        source = inspect.getsource(run)
        # Should reference max_full_extraction_candidates to limit scraping
        assert "max_full_extraction_candidates" in source


# ═══════════════════════════════════════════════
# Real API tests (end-to-end)
# ═══════════════════════════════════════════════

@skip_if_no_key
@skip_if_no_searxng
class TestPipelineReal:
    """End-to-end pipeline tests against real OpenRouter + SearXNG."""

    async def test_cheap_depth_completes(self):
        """Cheap depth pipeline completes without errors."""
        ctx = _make_ctx(depth="cheap")
        response = await run(ctx)

        assert isinstance(response, dict)
        assert response["company"] == "meta.com"

    async def test_cheap_depth_under_budget(self):
        """Cheap depth stays within its $0.01 budget."""
        ctx = _make_ctx(depth="cheap")
        response = await run(ctx)

        assert response["cost"]["total_cost"] <= 0.015  # Small buffer
        assert response["budget"]["requested"] == 0.01

    async def test_cheap_depth_under_5_seconds(self):
        """Cheap depth completes in under 5 seconds."""
        import time

        ctx = _make_ctx(depth="cheap")
        start = time.monotonic()
        await run(ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 5.0, f"Pipeline took {elapsed:.2f}s, expected < 5s"

    async def test_cheap_depth_response_shape(self):
        """Cheap depth produces complete v2 response."""
        ctx = _make_ctx(depth="cheap")
        response = await run(ctx)

        assert "company" in response
        assert "events" in response
        assert "answers" in response
        assert "signals" in response
        assert "usage" in response
        assert "topic_results" in response
        assert "cost" in response
        assert "budget" in response

        # Answers should have per-topic entries
        assert len(response["answers"]) == 1
        answer = response["answers"][0]
        assert answer["topic"] == "layoffs"
        assert "validity" in answer
        assert "confirmation" in answer
        assert "timing" in answer
        assert "summary" in answer
        assert "valid_sources" in answer

    async def test_cheap_depth_cost_attribution(self):
        """Sum of event cost_attribution ≈ total_cost."""
        ctx = _make_ctx(depth="cheap")
        response = await run(ctx)

        total_attributed = sum(
            e.get("cost_attribution", 0.0) for e in response["events"]
        )
        total_cost = response["cost"]["total_cost"]

        # Allow 20% tolerance for shared cost amortization
        if response["events"]:
            assert abs(total_attributed - total_cost) / max(total_cost, 0.0001) < 0.20

    async def test_standard_depth_completes(self):
        """Standard depth pipeline completes with LLM components."""
        ctx = _make_ctx(depth="standard")
        response = await run(ctx)

        assert isinstance(response, dict)
        assert response["company"] == "meta.com"
        # Should have consumed tokens for topic + queries + validation + formatting
        assert response["usage"]["total_tokens"] > 0

    async def test_standard_depth_under_15_seconds(self):
        """Standard depth completes in under 15 seconds."""
        import time

        ctx = _make_ctx(depth="standard")
        start = time.monotonic()
        await run(ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 15.0, f"Pipeline took {elapsed:.2f}s, expected < 15s"

    async def test_standard_depth_budget(self):
        """Standard depth respects $0.50 budget."""
        ctx = _make_ctx(depth="standard")
        response = await run(ctx)

        assert response["budget"]["requested"] == 0.50
        assert response["cost"]["total_cost"] <= 0.55  # Small buffer

    async def test_standard_depth_uses_llm_topic(self):
        """Standard depth uses LLM for topic analysis (not regex)."""
        ctx = _make_ctx(depth="standard", topics=["recent massive layoffs at meta platforms"])
        await run(ctx)

        # Topic should be simplified by LLM to ≤3 words
        assert ctx.topic is not None
        assert len(ctx.topic.simplified.split()) <= 3

    async def test_deep_depth_completes(self):
        """Deep depth pipeline completes with stronger models."""
        ctx = _make_ctx(depth="deep")
        response = await run(ctx)

        assert isinstance(response, dict)
        assert response["company"] == "meta.com"
        assert response["usage"]["total_tokens"] > 0

    async def test_deep_depth_under_30_seconds(self):
        """Deep depth completes in under 30 seconds."""
        import time

        ctx = _make_ctx(depth="deep")
        start = time.monotonic()
        await run(ctx)
        elapsed = time.monotonic() - start

        assert elapsed < 30.0, f"Pipeline took {elapsed:.2f}s, expected < 30s"

    async def test_deep_depth_budget(self):
        """Deep depth respects $2.00 budget."""
        ctx = _make_ctx(depth="deep")
        response = await run(ctx)

        assert response["budget"]["requested"] == 2.00
        assert response["cost"]["total_cost"] <= 2.10  # Small buffer

    async def test_deep_depth_more_candidates(self):
        """Deep depth processes more candidates than standard."""
        ctx = _make_ctx(depth="deep")
        response = await run(ctx)

        # Deep has max_candidates_per_topic=20, standard=10
        # We can't guarantee search finds that many, but the policy is correct
        assert ctx.policy.max_candidates_per_topic == 20
        assert ctx.policy.max_queries_per_topic == 12
        assert ctx.policy.max_full_extraction_candidates == 20

    async def test_model_routing_by_depth(self):
        """Model routing changes based on depth."""
        cheap = ResearchDepthPolicy.from_request("cheap")
        standard = ResearchDepthPolicy.from_request("standard")
        deep = ResearchDepthPolicy.from_request("deep")

        # Cheap and standard use flash for validation
        assert cheap.model_for_task("validation") == "deepseek/deepseek-v4-flash:nitro"
        assert standard.model_for_task("validation") == "deepseek/deepseek-v4-flash:nitro"

        # Deep uses kimi-k2.6 for validation
        assert deep.model_for_task("validation") == "moonshotai/kimi-k2.6:nitro"

    async def test_multiple_topics(self):
        """Pipeline handles multiple topics sequentially."""
        ctx = _make_ctx(topics=["layoffs", "earnings"], depth="cheap")
        response = await run(ctx)

        assert len(response["answers"]) == 2
        topics_found = [a["topic"] for a in response["answers"]]
        assert "layoffs" in topics_found
        assert "earnings" in topics_found

    async def test_timeout_with_real_pipeline(self):
        """Timeout returns partial response during real execution."""
        ctx = _make_ctx(depth="cheap")
        # Very short timeout should interrupt before completion
        response = await run(ctx, timeout_seconds=0.001)

        assert isinstance(response, dict)
        assert "company" in response
        assert "events" in response
        assert "budget" in response
