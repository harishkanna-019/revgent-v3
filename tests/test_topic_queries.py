"""Topic analysis + query generation tool tests.

- Pure parsing tests run without infrastructure (milliseconds).
- Real API tests call OpenRouter, skipped when OPENROUTER_API_KEY is missing.
"""

import os

import pytest

from core.context import RunContext, TopicState
from core.depth import ResearchDepthPolicy
from core.types import ToolResult
from tools.topic import _parse_keyword_list, analyze
from tools.queries import (
    _brand_phrase_candidates,
    _dedupe_preserving_order,
    _enforce_phrase_quoting,
    _parse_query_list,
    generate,
)

pytestmark = pytest.mark.asyncio

skip_if_no_key = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)


# ── Helpers ──


def _make_ctx(topic: str, depth: str = "cheap") -> RunContext:
    """Create a RunContext with the given topic."""
    policy = ResearchDepthPolicy.from_request(depth)
    ctx = RunContext(
        policy=policy,
        company="meta.com",
        topics=[topic],
        date_min=0,
        date_max=90,
    )
    ctx.topic = TopicState(original=topic)
    return ctx


# ── _parse_keyword_list (pure) ──


class TestParseKeywordList:
    def test_empty_string(self):
        assert _parse_keyword_list("") == []

    def test_json_array(self):
        text = '["layoffs", "job cuts", "workforce reduction"]'
        assert _parse_keyword_list(text) == [
            "layoffs",
            "job cuts",
            "workforce reduction",
        ]

    def test_markdown_code_block(self):
        text = '```json\n["layoffs", "job cuts"]\n```'
        assert _parse_keyword_list(text) == ["layoffs", "job cuts"]

    def test_extra_text_around_json(self):
        text = 'Here are the keywords:\n```\n["layoffs", "cuts"]\n```\nHope that helps!'
        assert _parse_keyword_list(text) == ["layoffs", "cuts"]

    def test_no_json_fallback_to_lines(self):
        text = "layoffs\njob cuts\nworkforce reduction"
        assert _parse_keyword_list(text) == [
            "layoffs",
            "job cuts",
            "workforce reduction",
        ]

    def test_comma_separated_fallback(self):
        text = "layoffs, job cuts, workforce reduction"
        assert _parse_keyword_list(text) == [
            "layoffs",
            "job cuts",
            "workforce reduction",
        ]

    def test_deduplication(self):
        text = '["layoffs", "layoffs", "Job Cuts", "job cuts"]'
        assert _parse_keyword_list(text) == ["layoffs", "job cuts"]

    def test_empty_items_filtered(self):
        text = '["layoffs", "", "cuts", "  "]'
        assert _parse_keyword_list(text) == ["layoffs", "cuts"]

    def test_non_string_items_filtered(self):
        text = '["layoffs", 123, "cuts", null]'
        assert _parse_keyword_list(text) == ["layoffs", "cuts"]

    def test_case_normalization(self):
        text = '["Layoffs", "JOB CUTS", "Workforce Reduction"]'
        assert _parse_keyword_list(text) == [
            "layoffs",
            "job cuts",
            "workforce reduction",
        ]

    def test_mixed_valid_invalid(self):
        text = 'Some intro text ["layoffs", "cuts"] extra stuff'
        assert _parse_keyword_list(text) == ["layoffs", "cuts"]


# ── _parse_query_list (pure) ──


class TestParseQueryList:
    def test_empty_string(self):
        assert _parse_query_list("") == []

    def test_json_array(self):
        text = '["meta layoffs", "meta job cuts"]'
        assert _parse_query_list(text) == ["meta layoffs", "meta job cuts"]

    def test_markdown_code_block(self):
        text = '```json\n["meta layoffs", "meta cuts"]\n```'
        assert _parse_query_list(text) == ["meta layoffs", "meta cuts"]

    def test_extra_text_around_json(self):
        text = 'Here are queries:\n```\n["meta layoffs", "meta cuts"]\n```'
        assert _parse_query_list(text) == ["meta layoffs", "meta cuts"]

    def test_no_json_fallback_to_lines(self):
        text = "meta layoffs\nmeta job cuts\nmeta workforce reduction"
        assert _parse_query_list(text) == [
            "meta layoffs",
            "meta job cuts",
            "meta workforce reduction",
        ]

    def test_deduplication(self):
        text = '["meta layoffs", "meta layoffs", "Meta Job Cuts"]'
        # First occurrence preserved, dedup is case-insensitive
        assert _parse_query_list(text) == ["meta layoffs", "Meta Job Cuts"]

    def test_bullet_points_fallback(self):
        text = "- meta layoffs\n* meta job cuts\n• meta restructuring"
        assert _parse_query_list(text) == [
            "meta layoffs",
            "meta job cuts",
            "meta restructuring",
        ]

    def test_empty_items_filtered(self):
        text = '["meta layoffs", "", "meta cuts"]'
        assert _parse_query_list(text) == ["meta layoffs", "meta cuts"]

    def test_case_preservation(self):
        """Queries preserve original case (dedup is case-insensitive)."""
        text = '["Meta Layoffs", "meta layoffs", "META EARNINGS"]'
        # First occurrence preserved, duplicates (case-insensitive) skipped
        assert _parse_query_list(text) == ["Meta Layoffs", "META EARNINGS"]


class TestBrandPhraseCandidates:
    """Detecting which brand names need phrase-quoting.

    Single-token brands ('meta', 'twitch') don't benefit from quoting.
    Multi-token brands ('under armour', 'best western') MUST be quoted
    or the engines match each word independently, dropping precision
    by ~4x in our measurements.
    """

    def test_domain_with_tld(self):
        """Common TLDs stripped, then quoting decided on the remainder."""
        assert _brand_phrase_candidates("meta.com") == []
        assert _brand_phrase_candidates("underarmour.com") == []
        # .tv isn't in the strip list; twitch.tv stays as one token (no space).
        assert _brand_phrase_candidates("twitch.tv") == []

    def test_multi_word_brand(self):
        assert _brand_phrase_candidates("under armour") == ["under armour"]
        assert _brand_phrase_candidates("best western") == ["best western"]
        assert _brand_phrase_candidates("general motors") == ["general motors"]

    def test_case_normalized(self):
        assert _brand_phrase_candidates("Under Armour") == ["under armour"]
        assert _brand_phrase_candidates("WELLS FARGO") == ["wells fargo"]

    def test_empty(self):
        assert _brand_phrase_candidates("") == []
        assert _brand_phrase_candidates("   ") == []

    def test_known_tlds_stripped(self):
        for suffix in (".com", ".io", ".co", ".net", ".org", ".ai", ".app"):
            assert _brand_phrase_candidates(f"meta{suffix}") == []
            assert _brand_phrase_candidates(f"under armour{suffix}") == ["under armour"]


class TestEnforcePhraseQuoting:
    """Deterministic safety net for the LLM's phrase-quoting compliance.

    Even with the operator explicitly requested in the prompt, smaller
    models sometimes return queries like 'under armour breach' (unquoted).
    The post-processor wraps every unquoted multi-word brand mention so
    the operator guarantee is independent of model behaviour.
    """

    def test_unquoted_brand_gets_quoted(self):
        result = _enforce_phrase_quoting("under armour breach", ["under armour"])
        assert result == '"under armour" breach'

    def test_already_quoted_brand_unchanged(self):
        """Don't over-quote when LLM already produced the right output."""
        result = _enforce_phrase_quoting('"under armour" data breach', ["under armour"])
        assert result == '"under armour" data breach'

    def test_mixed_quoted_and_unquoted_brand_mentions(self):
        """A query like '"under armour" vs under armour' should end up
        with BOTH mentions quoted (the unquoted one gets wrapped)."""
        result = _enforce_phrase_quoting(
            '"under armour" breach OR under armour hack', ["under armour"]
        )
        # First mention preserved; second mention wrapped.
        assert '"under armour" breach OR "under armour" hack' == result

    def test_case_insensitive_match(self):
        """Case-insensitive match. The wrapped output uses the canonical
        (lowercase) form because search engines are case-insensitive and
        the canonical form keeps cache keys consistent."""
        result = _enforce_phrase_quoting("Under Armour breach", ["under armour"])
        assert result == '"under armour" breach'
        # Also handle mixed UPPER and lower in the same query.
        result = _enforce_phrase_quoting(
            "UNDER ARMOUR and under armour", ["under armour"]
        )
        assert result == '"under armour" and "under armour"'

    def test_single_word_brand_no_op(self):
        """Single-token brands aren't in the candidate list, so the
        post-processor is a no-op for them."""
        result = _enforce_phrase_quoting("meta layoffs", [])
        assert result == "meta layoffs"

    def test_partial_word_not_wrapped(self):
        """'armour' inside 'armoured' must NOT trigger the wrap.

        Regression: if the regex was naive (no word boundary check) it
        would corrupt 'armoured' into 'arm\"our\"ed' or similar.
        """
        result = _enforce_phrase_quoting("armoured vehicle launch", ["under armour"])
        assert result == "armoured vehicle launch"

    def test_no_brand_candidates(self):
        """Empty candidate list returns query unchanged."""
        assert _enforce_phrase_quoting("meta layoffs", []) == "meta layoffs"

    def test_empty_query(self):
        assert _enforce_phrase_quoting("", ["under armour"]) == ""

    def test_multiple_brand_candidates(self):
        """Both candidates wrapped independently."""
        result = _enforce_phrase_quoting(
            "under armour vs best western shoe deal",
            ["under armour", "best western"],
        )
        assert result == '"under armour" vs "best western" shoe deal'

    def test_brand_at_start_and_end(self):
        """Boundary positions in the string are handled."""
        assert (
            _enforce_phrase_quoting("under armour", ["under armour"])
            == '"under armour"'
        )
        assert (
            _enforce_phrase_quoting("breach at under armour", ["under armour"])
            == 'breach at "under armour"'
        )

    def test_brand_inside_boolean_or_clause(self):
        """Brand mentions inside an OR clause also get wrapped if unquoted."""
        result = _enforce_phrase_quoting(
            'under armour (breach OR hack OR "data leak")',
            ["under armour"],
        )
        assert result == '"under armour" (breach OR hack OR "data leak")'


class TestDedupePreservingOrder:
    def test_preserves_order(self):
        assert _dedupe_preserving_order(["a", "b", "c"]) == ["a", "b", "c"]

    def test_drops_case_insensitive_duplicates(self):
        assert _dedupe_preserving_order(
            ['"meta" layoffs', '"Meta" Layoffs', '"meta" earnings']
        ) == ['"meta" layoffs', '"meta" earnings']

    def test_drops_whitespace_only_duplicates(self):
        assert _dedupe_preserving_order(['"meta" layoffs', '  "meta" layoffs  ']) == [
            '"meta" layoffs'
        ]

    def test_drops_empties(self):
        assert _dedupe_preserving_order(["", "a", "", "b"]) == ["a", "b"]


# ── analyze() real API tests ──


@skip_if_no_key
class TestAnalyzeReal:
    """Tests that call real OpenRouter for topic analysis."""

    async def test_short_topic_skips_simplification(self):
        """Topics with ≤3 words are not simplified via LLM."""
        ctx = _make_ctx("layoffs")
        result = await analyze(ctx)

        assert isinstance(result, ToolResult)
        assert result.output["simplified"] == "layoffs"
        assert isinstance(result.output["keywords"], list)
        assert len(result.output["keywords"]) > 0
        assert result.usage["total_tokens"] > 0

    async def test_long_topic_simplifies(self):
        """Topics with >3 words are simplified via LLM."""
        ctx = _make_ctx("recent massive layoffs at meta platforms")
        result = await analyze(ctx)

        assert isinstance(result, ToolResult)
        simplified = result.output["simplified"]
        assert simplified
        assert len(simplified.split()) <= 3
        assert isinstance(result.output["keywords"], list)
        assert len(result.output["keywords"]) > 0

    async def test_keywords_cached(self):
        """Calling analyze twice with same topic returns cached keywords."""
        ctx1 = _make_ctx("earnings report")
        result1 = await analyze(ctx1)

        ctx2 = _make_ctx("earnings report")
        result2 = await analyze(ctx2)

        # Second call should reuse cached keywords (no extra tokens for keywords)
        # But simplification still happens (not cached)
        assert result1.output["keywords"] == result2.output["keywords"]

    async def test_empty_topic(self):
        """Empty topic returns empty result."""
        ctx = _make_ctx("")
        result = await analyze(ctx)

        assert result.output["simplified"] == ""
        assert result.output["keywords"] == []
        assert result.usage["total_tokens"] == 0

    async def test_cost_recorded_on_context(self):
        """Analyze records LLM cost on the context."""
        ctx = _make_ctx("product launch announcements")
        initial_cost = ctx.cost.total_cost
        await analyze(ctx)
        assert ctx.cost.total_cost > initial_cost

    async def test_toolresult_shape(self):
        """Result has correct ToolResult shape."""
        ctx = _make_ctx("layoffs")
        result = await analyze(ctx)

        assert hasattr(result, "output")
        assert hasattr(result, "usage")
        assert "simplified" in result.output
        assert "keywords" in result.output
        assert "input_tokens" in result.usage
        assert "output_tokens" in result.usage
        assert "total_tokens" in result.usage


# ── generate() real API tests ──


@skip_if_no_key
class TestGenerateReal:
    """Tests that call real OpenRouter for query generation."""

    async def test_generates_queries(self):
        """Generate returns a list of query strings."""
        ctx = _make_ctx("layoffs")
        ctx.topic.simplified = "layoffs"
        result = await generate(ctx)

        assert isinstance(result, ToolResult)
        queries = result.output
        assert isinstance(queries, list)
        assert len(queries) > 0
        assert all(isinstance(q, str) for q in queries)
        assert result.usage["total_tokens"] > 0

    async def test_queries_limited_by_policy(self):
        """Number of queries respects max_queries_per_topic."""
        ctx = _make_ctx("layoffs", depth="cheap")
        ctx.topic.simplified = "layoffs"
        result = await generate(ctx)

        max_queries = ctx.policy.max_queries_per_topic
        assert len(result.output) <= max_queries

    async def test_uses_simplified_topic(self):
        """generate() uses ctx.topic.simplified when available."""
        ctx = _make_ctx("recent massive layoffs at meta platforms")
        ctx.topic.simplified = "layoffs"
        result = await generate(ctx)

        queries = result.output
        assert len(queries) > 0

    async def test_empty_company_fallback(self):
        """Empty company/topic returns fallback query."""
        ctx = _make_ctx("layoffs")
        ctx.company = ""
        result = await generate(ctx)

        assert result.output == ["layoffs"]

    async def test_empty_topic_fallback(self):
        """Empty topic returns fallback query."""
        ctx = _make_ctx("")
        ctx.topic.simplified = ""
        result = await generate(ctx)

        assert result.output == ["meta.com"]

    async def test_cost_recorded_on_context(self):
        """Generate records LLM cost on the context."""
        ctx = _make_ctx("layoffs")
        ctx.topic.simplified = "layoffs"
        initial_cost = ctx.cost.total_cost
        await generate(ctx)
        assert ctx.cost.total_cost > initial_cost

    async def test_toolresult_shape(self):
        """Result has correct ToolResult shape."""
        ctx = _make_ctx("layoffs")
        ctx.topic.simplified = "layoffs"
        result = await generate(ctx)

        assert hasattr(result, "output")
        assert hasattr(result, "usage")
        assert isinstance(result.output, list)
        assert "input_tokens" in result.usage
        assert "output_tokens" in result.usage
        assert "total_tokens" in result.usage
