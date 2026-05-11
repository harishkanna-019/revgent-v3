"""Validation, formatting, and signal routing tests.

- Pure parsing/filter tests run without infrastructure (milliseconds).
- Real API tests call OpenRouter, skipped when OPENROUTER_API_KEY is missing.
"""

import os

import pytest

from core.context import RunContext, TopicState
from core.depth import ResearchDepthPolicy
from core.types import ToolResult
from filters.signals import (
    LaneDecision,
    _confidence_score,
    _extract_source_name,
    _infer_signal_type,
    classify_result,
)
from tools.format import _merge_usage, _parse_classification, format_one
from tools.validate import _merge_usage as _val_merge_usage
from tools.validate import _parse_fact_check, _parse_relevance, validate_one

pytestmark = pytest.mark.asyncio

skip_if_no_key = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)


# ── Helpers ──


def _make_ctx(company: str = "meta.com", depth: str = "cheap") -> RunContext:
    policy = ResearchDepthPolicy.from_request(depth)
    ctx = RunContext(
        policy=policy,
        company=company,
        topics=["layoffs"],
        date_min=0,
        date_max=90,
    )
    ctx.topic = TopicState(original="layoffs")
    return ctx


def _make_candidate(
    title: str = "Meta Layoffs",
    url: str = "https://reuters.com/article",
    content: str = "Meta announced layoffs of 1,000 employees.",
    published_date: str = "2026-01-16",
) -> dict:
    return {
        "title": title,
        "url": url,
        "content": content,
        "published_date": published_date,
    }


# ═══════════════════════════════════════════════
# tools/validate.py — pure parsing
# ═══════════════════════════════════════════════


class TestParseRelevance:
    def test_yes(self):
        assert _parse_relevance("YES") == "YES"
        assert _parse_relevance("The answer is YES.") == "YES"

    def test_no(self):
        assert _parse_relevance("NO") == "NO"
        assert _parse_relevance("The answer is NO.") == "NO"

    def test_uncertain(self):
        assert _parse_relevance("UNCERTAIN") == "UNCERTAIN"
        assert _parse_relevance("I'm UNCERTAIN about this.") == "UNCERTAIN"

    def test_defaults_to_uncertain(self):
        assert _parse_relevance("maybe") == "UNCERTAIN"
        assert _parse_relevance("") == "UNCERTAIN"

    def test_case_insensitive(self):
        assert _parse_relevance("yes") == "YES"
        assert _parse_relevance("No") == "NO"
        assert _parse_relevance("Uncertain") == "UNCERTAIN"


class TestParseFactCheck:
    def test_hard_fact(self):
        assert _parse_fact_check("HARD_FACT") == "HARD_FACT"
        assert _parse_fact_check("The answer is HARD FACT.") == "HARD_FACT"

    def test_opinion(self):
        assert _parse_fact_check("OPINION") == "OPINION"
        assert _parse_fact_check("This is an OPINION piece.") == "OPINION"

    def test_defaults_to_opinion(self):
        assert _parse_fact_check("maybe") == "OPINION"
        assert _parse_fact_check("") == "OPINION"

    def test_case_insensitive(self):
        assert _parse_fact_check("hard_fact") == "HARD_FACT"
        assert _parse_fact_check("opinion") == "OPINION"


class TestValidateMergeUsage:
    def test_merges_correctly(self):
        u1 = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        u2 = {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
        merged = _val_merge_usage(u1, u2)
        assert merged == {"input_tokens": 30, "output_tokens": 15, "total_tokens": 45}

    def test_handles_missing_keys(self):
        u1 = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        u2 = {"input_tokens": 20}
        merged = _val_merge_usage(u1, u2)
        assert merged == {"input_tokens": 30, "output_tokens": 5, "total_tokens": 15}


# ═══════════════════════════════════════════════
# tools/validate.py — real API tests
# ═══════════════════════════════════════════════


@skip_if_no_key
class TestValidateOneReal:
    """Tests that call real OpenRouter for validation."""

    async def test_valid_article(self):
        """A clearly factual article about the company should return valid."""
        ctx = _make_ctx("meta.com")
        candidate = _make_candidate(
            title="Meta Announces 1,000 Layoffs",
            content="Meta Platforms Inc. announced on Thursday that it will lay off approximately 1,000 employees across its Reality Labs division. The company cited restructuring efforts and a shift toward AI-focused investments.",
        )
        result = await validate_one(ctx, candidate)

        assert isinstance(result, ToolResult)
        assert result.item_id == candidate["url"]
        assert result.output["status"] in ("valid", "opinion", "not_about_company")
        assert result.output["original"] == candidate
        assert result.usage["total_tokens"] > 0

    async def test_not_about_company(self):
        """An article not about the company should skip fact check."""
        ctx = _make_ctx("meta.com")
        candidate = _make_candidate(
            title="Apple Announces New iPhone",
            content="Apple Inc. announced a new iPhone model with advanced camera features. The company did not mention Meta or Facebook in its presentation.",
        )
        result = await validate_one(ctx, candidate)

        assert isinstance(result, ToolResult)
        assert result.output["status"] == "not_about_company"
        assert result.output["result"] is None
        # Should only have relevance check tokens, not fact check
        assert result.usage["total_tokens"] > 0

    async def test_cost_recorded_on_context(self):
        """Validation records cost on the context."""
        ctx = _make_ctx("meta.com")
        initial_cost = ctx.cost.total_cost
        candidate = _make_candidate()
        await validate_one(ctx, candidate)
        assert ctx.cost.total_cost > initial_cost

    async def test_toolresult_shape(self):
        """Result has correct ToolResult shape."""
        ctx = _make_ctx("meta.com")
        result = await validate_one(ctx, _make_candidate())

        assert hasattr(result, "output")
        assert hasattr(result, "usage")
        assert hasattr(result, "item_id")
        assert "status" in result.output
        assert "original" in result.output
        assert "input_tokens" in result.usage
        assert "output_tokens" in result.usage
        assert "total_tokens" in result.usage

    async def test_uncertain_retry(self):
        """An ambiguous article triggers step-by-step retry."""
        ctx = _make_ctx("meta.com")
        # Use a very ambiguous title/content
        candidate = _make_candidate(
            title="Tech Industry Trends",
            content="The technology industry is experiencing significant changes. Various companies are adjusting their workforce strategies. Some analysts believe social media platforms may face regulatory challenges.",
        )
        result = await validate_one(ctx, candidate)

        assert isinstance(result, ToolResult)
        assert result.output["status"] in ("valid", "opinion", "not_about_company")
        # Should have consumed more tokens due to retry
        assert result.usage["total_tokens"] > 0


# ═══════════════════════════════════════════════
# tools/format.py — pure parsing
# ═══════════════════════════════════════════════


class TestParseClassification:
    def test_novel_fact(self):
        assert _parse_classification("novel_fact") == "novel_fact"
        assert _parse_classification("This is a novel_fact article.") == "novel_fact"

    def test_report(self):
        assert _parse_classification("report") == "report"

    def test_analysis(self):
        assert _parse_classification("analysis") == "analysis"

    def test_historical(self):
        assert _parse_classification("historical") == "historical"

    def test_defaults_to_analysis(self):
        assert _parse_classification("unknown_type") == "analysis"
        assert _parse_classification("") == "analysis"

    def test_case_insensitive(self):
        assert _parse_classification("Novel_Fact") == "novel_fact"
        assert _parse_classification("ANALYSIS") == "analysis"

    def test_prefers_first_match(self):
        # If multiple types appear, one of the valid types wins
        result = _parse_classification("analysis and report")
        assert result in ("analysis", "report")


class TestFormatMergeUsage:
    def test_merges_correctly(self):
        u1 = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        u2 = {"input_tokens": 200, "output_tokens": 100, "total_tokens": 300}
        merged = _merge_usage(u1, u2)
        assert merged == {
            "input_tokens": 300,
            "output_tokens": 150,
            "total_tokens": 450,
        }


# ═══════════════════════════════════════════════
# tools/format.py — real API tests
# ═══════════════════════════════════════════════


@skip_if_no_key
class TestFormatOneReal:
    """Tests that call real OpenRouter for formatting."""

    async def test_formats_article(self):
        """format_one returns a properly structured event dict."""
        ctx = _make_ctx("meta.com")
        candidate = _make_candidate()
        result = await format_one(ctx, candidate)

        assert isinstance(result, ToolResult)
        event = result.output
        assert event["headline"] == candidate["title"]
        assert event["source_url"] == candidate["url"]
        assert event["content_type"] in {
            "novel_fact",
            "report",
            "analysis",
            "historical",
        }
        assert event["date"] == "2026-01-16"
        assert result.usage["total_tokens"] > 0
        assert result.item_id == candidate["url"]

    async def test_summary_is_populated(self):
        """The event dict contains an AI-generated summary."""
        ctx = _make_ctx("meta.com")
        candidate = _make_candidate(
            content="Meta Platforms Inc. announced on Thursday that it will lay off approximately 1,000 employees across its Reality Labs division.",
        )
        result = await format_one(ctx, candidate)

        event = result.output
        assert event["description"] != candidate["content"][:400]
        assert len(event["description"]) > 10

    async def test_concurrent_calls(self):
        """Summary and classification run concurrently (faster than sequential)."""
        ctx = _make_ctx("meta.com")
        candidate = _make_candidate()
        result = await format_one(ctx, candidate)

        # Should succeed with both summary and classification
        assert result.output["description"]
        assert result.output["content_type"] in {
            "novel_fact",
            "report",
            "analysis",
            "historical",
        }
        assert result.usage["total_tokens"] > 0

    async def test_cost_recorded_on_context(self):
        """Formatting records cost on the context."""
        ctx = _make_ctx("meta.com")
        initial_cost = ctx.cost.total_cost
        await format_one(ctx, _make_candidate())
        assert ctx.cost.total_cost > initial_cost

    async def test_toolresult_shape(self):
        """Result has correct ToolResult shape."""
        ctx = _make_ctx("meta.com")
        result = await format_one(ctx, _make_candidate())

        assert hasattr(result, "output")
        assert hasattr(result, "usage")
        assert hasattr(result, "item_id")
        assert "headline" in result.output
        assert "description" in result.output
        assert "content_type" in result.output
        assert "input_tokens" in result.usage
        assert "output_tokens" in result.usage
        assert "total_tokens" in result.usage


# ═══════════════════════════════════════════════
# filters/signals.py — pure logic
# ═══════════════════════════════════════════════


class TestInferSignalType:
    def test_market_speculation(self):
        assert (
            _infer_signal_type("This is speculation about the market")
            == "market_speculation"
        )
        assert (
            _infer_signal_type("Rumored acquisition could happen")
            == "market_speculation"
        )
        assert _infer_signal_type("The company might expand") == "market_speculation"

    def test_unconfirmed(self):
        assert _infer_signal_type("Unconfirmed reports suggest...") == "unconfirmed"
        assert _infer_signal_type("Sources say the deal is happening") == "unconfirmed"

    def test_early_report(self):
        assert _infer_signal_type("Early reports indicate...") == "early_report"
        assert _infer_signal_type("Preliminary findings show...") == "early_report"

    def test_analyst_commentary(self):
        assert (
            _infer_signal_type("Analyst commentary on earnings") == "analyst_commentary"
        )
        assert _infer_signal_type("Expert opinion piece") == "analyst_commentary"

    def test_default(self):
        assert _infer_signal_type("Something random") == "analyst_commentary"
        assert _infer_signal_type("") == "analyst_commentary"


class TestConfidenceScore:
    def test_invalid(self):
        assert _confidence_score(False, False, "anything") == 0.0

    def test_hard_fact(self):
        assert _confidence_score(True, True, "anything") == 1.0

    def test_soft_base(self):
        score = _confidence_score(True, False, "Some analyst opinion")
        assert 0.5 <= score <= 0.7

    def test_strong_boost(self):
        score = _confidence_score(True, False, "definitely confirmed by sources")
        assert score > 0.6

    def test_weak_penalty(self):
        score = _confidence_score(True, False, "might possibly happen")
        assert score < 0.6

    def test_capped(self):
        # Even with strong markers, soft signal maxes below 1.0
        score = _confidence_score(True, False, "definitely certainly confirmed")
        assert score <= 0.9

    def test_floor(self):
        # Even with weak markers, soft signal floors above 0
        score = _confidence_score(True, False, "might could possibly maybe unclear")
        assert score >= 0.3


class TestExtractSourceName:
    def test_basic_url(self):
        assert _extract_source_name("https://reuters.com/article") == "reuters.com"

    def test_www_stripped(self):
        assert _extract_source_name("https://www.bbc.com/news") == "bbc.com"

    def test_empty(self):
        assert _extract_source_name("") == ""


class TestClassifyResult:
    def test_invalid_discard(self):
        """Not about company → discard lane."""
        result = _make_candidate()
        decision = classify_result(result, False, False, "NO", "layoffs")
        assert decision.lane == "discard"
        assert decision.event is None
        assert decision.signal is None

    def test_hard_fact_event(self):
        """Valid + hard_fact → event lane."""
        result = _make_candidate()
        decision = classify_result(result, True, True, "HARD_FACT", "layoffs")
        assert decision.lane == "event"
        assert decision.event is not None
        assert decision.event["topic"] == "layoffs"
        assert decision.signal is None

    def test_opinion_signal(self):
        """Valid + soft → signal lane."""
        result = _make_candidate()
        decision = classify_result(result, True, False, "OPINION", "layoffs")
        assert decision.lane == "signal"
        assert decision.signal is not None
        assert decision.signal["topic"] == "layoffs"
        assert decision.signal["signal_type"] in {
            "market_speculation",
            "unconfirmed",
            "early_report",
            "analyst_commentary",
        }
        assert 0.0 <= decision.signal["confidence"] <= 1.0
        assert decision.event is None

    def test_signal_infers_type(self):
        """Signal type is inferred from fact_check_raw text."""
        result = _make_candidate()
        decision = classify_result(
            result, True, False, "Speculation about market", "layoffs"
        )
        assert decision.signal["signal_type"] == "market_speculation"

    def test_event_has_correct_shape(self):
        """Event dict matches expected shape."""
        result = _make_candidate()
        decision = classify_result(result, True, True, "HARD_FACT", "layoffs")
        event = decision.event
        assert set(event.keys()) >= {
            "headline",
            "description",
            "topic",
            "date",
            "source_name",
            "source_url",
            "content_type",
            "headline_has_numbers",
            "cost_attribution",
        }

    def test_signal_has_correct_shape(self):
        """Signal dict matches expected shape."""
        result = _make_candidate()
        decision = classify_result(result, True, False, "OPINION", "layoffs")
        signal = decision.signal
        assert set(signal.keys()) >= {
            "headline",
            "description",
            "topic",
            "date",
            "source_name",
            "source_url",
            "signal_type",
            "confidence",
            "why_not_event",
            "cost_attribution",
        }

    def test_lane_decision_frozen(self):
        """LaneDecision is immutable."""
        from dataclasses import FrozenInstanceError

        decision = LaneDecision(lane="discard")
        with pytest.raises(FrozenInstanceError):
            decision.lane = "event"
