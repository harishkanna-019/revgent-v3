"""Validation tool: company relevance check + fact check via LLM."""

from core.context import RunContext
from core.types import ToolResult
from formatting import parse_date
from providers import llm

_RELEVANCE_PROMPT = """You are evaluating whether a news article is specifically about a company.

Company: {company}
Article title: {title}
Article content: {content}

Is this article specifically about {company}? Answer with exactly one word:
- YES: The article is clearly about {company}
- NO: The article is not about {company} (mentions them only in passing, or is about a different company)
- UNCERTAIN: You cannot tell if it's about {company}

Your answer must be exactly YES, NO, or UNCERTAIN."""

_RELEVANCE_RETRY_PROMPT = """You are evaluating whether a news article is specifically about a company.

Company: {company}
Article title: {title}
Article content: {content}

You previously said you were UNCERTAIN. Now think step by step:
1. Does the article mention {company} in the headline or first paragraph?
2. Is {company} the main subject or just mentioned in passing?
3. Does the article discuss {company}'s actions, products, or news?

Based on this reasoning, what is your final answer? Answer with exactly one word:
- YES: The article is clearly about {company}
- NO: The article is not about {company}

Your answer must be exactly YES or NO."""

_FACT_CHECK_PROMPT = """You are evaluating whether a news article contains hard facts or opinions/speculation.

Article title: {title}
Article content: {content}

Is this article reporting verifiable facts or expressing opinions/speculation? Answer with exactly one word:
- HARD_FACT: The article reports concrete, verifiable facts (e.g., announced layoffs, published earnings, confirmed product launch)
- OPINION: The article contains analysis, prediction, speculation, or opinion (e.g., "analysts believe", "might", "could", "rumored")

Your answer must be exactly HARD_FACT or OPINION."""


def _parse_relevance(text: str) -> str:
    """Parse relevance response into YES/NO/UNCERTAIN."""
    text = text.strip().upper()
    # Extract first occurrence of YES/NO/UNCERTAIN
    for word in ["YES", "NO", "UNCERTAIN"]:
        if word in text:
            return word
    return "UNCERTAIN"


def _parse_fact_check(text: str) -> str:
    """Parse fact-check response into HARD_FACT/OPINION."""
    text = text.strip().upper()
    if "HARD_FACT" in text or "HARD FACT" in text:
        return "HARD_FACT"
    if "OPINION" in text:
        return "OPINION"
    # Default to opinion if unclear
    return "OPINION"


def _merge_usage(u1: dict, u2: dict) -> dict:
    """Merge two usage dicts."""
    return {
        "input_tokens": u1.get("input_tokens", 0) + u2.get("input_tokens", 0),
        "output_tokens": u1.get("output_tokens", 0) + u2.get("output_tokens", 0),
        "total_tokens": u1.get("total_tokens", 0) + u2.get("total_tokens", 0),
    }


async def validate_one(ctx: RunContext, candidate: dict) -> ToolResult:
    """Validate a candidate article for company relevance and factual content.

    Flow:
    1. Relevance check: Is this article about the company?
       - YES → proceed to fact check
       - NO → status="not_about_company", skip fact check (saves cost)
       - UNCERTAIN → retry with step-by-step reasoning, then decide
    2. Fact check (only if relevant): Is this a hard fact or opinion?
       - HARD_FACT → status="valid"
       - OPINION → status="opinion"

    Args:
        ctx: RunContext with company name and policy
        candidate: Search result dict with title, url, content

    Returns:
        ToolResult with output={"result": dict|None, "status": str, "original": dict}
        Status is one of: "valid", "opinion", "not_about_company"
    """
    company = ctx.company.strip().lower() if ctx.company else ""
    title = candidate.get("title", "")
    content = candidate.get("content", "")
    url = candidate.get("url", "")
    item_id = url or candidate.get("title", "")[:50]

    model = ctx.policy.model_for_task("validation")

    # ── Step 1: Relevance check ──
    relevance_prompt = _RELEVANCE_PROMPT.format(
        company=company, title=title, content=content[:2000]
    )
    relevance_text, relevance_usage = await llm.call(
        model=model, max_tokens=32, prompt=relevance_prompt
    )
    relevance = _parse_relevance(relevance_text)

    total_usage = dict(relevance_usage)

    # Retry on UNCERTAIN
    if relevance == "UNCERTAIN":
        retry_prompt = _RELEVANCE_RETRY_PROMPT.format(
            company=company, title=title, content=content[:2000]
        )
        retry_text, retry_usage = await llm.call(
            model=model, max_tokens=64, prompt=retry_prompt
        )
        total_usage = _merge_usage(total_usage, retry_usage)
        relevance = _parse_relevance(retry_text)
        # After retry, UNCERTAIN defaults to NO (not_about_company)
        if relevance == "UNCERTAIN":
            relevance = "NO"

    # Not about company → skip fact check, return immediately
    if relevance == "NO":
        ctx.record(total_usage, item_id=item_id)
        return ToolResult(
            output={"result": None, "status": "not_about_company", "original": candidate},
            usage=total_usage,
            item_id=item_id,
        )

    # ── Step 2: Fact check ──
    fact_model = ctx.policy.model_for_task("fact_check")
    fact_prompt = _FACT_CHECK_PROMPT.format(title=title, content=content[:2000])
    fact_text, fact_usage = await llm.call(
        model=fact_model, max_tokens=32, prompt=fact_prompt
    )
    total_usage = _merge_usage(total_usage, fact_usage)

    fact_result = _parse_fact_check(fact_text)
    is_hard_fact = fact_result == "HARD_FACT"

    status = "valid" if is_hard_fact else "opinion"

    result = {
        "is_valid": True,
        "is_hard_fact": is_hard_fact,
        "relevance_raw": relevance_text.strip(),
        "fact_check_raw": fact_text.strip(),
    }

    ctx.record(total_usage, item_id=item_id)

    return ToolResult(
        output={"result": result, "status": status, "original": candidate},
        usage=total_usage,
        item_id=item_id,
    )
