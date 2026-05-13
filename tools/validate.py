"""Validation tool: company relevance check + fact check via LLM."""

from core.context import RunContext
from core.types import ToolResult
from providers import llm

_RELEVANCE_PROMPT = """You are deciding whether a news article reports on {company} itself doing or experiencing the topic "{topic}".

Company: {company}
Topic: {topic}
Article title: {title}
Article content: {content}

A YES answer requires:
1. {company} is a subject of the article (not just a passing mention), AND
2. The article reports on {company} itself doing or experiencing "{topic}"
   (e.g. {company}'s own layoffs, {company}'s own funding round, {company}'s own breach).

Multi-company articles are YES if {company} is one of the companies experiencing "{topic}":
  Example: "Top 10 Tech Exec Moves This Week — Stripe hires new CTO, Google VP leaves"
  -> answer YES when researching stripe.com C-suite executive changes.

A NO answer applies when:
- The article is about a DIFFERENT company doing "{topic}" and only mentions {company} in passing
  (example: "Meta announces 8,000 layoffs; Anthropic also held AI talks with the White House"
   -> answer NO when researching anthropic.com layoffs)
- {company} is cited as a researcher, study author, competitor, or comparison
- The article is general industry commentary that references {company} without
  reporting on a specific event at {company}

Answer with exactly one word:
- YES: The article reports on {company}'s own "{topic}"
- NO: It does not (either different company or only a passing mention of {company})
- UNCERTAIN: you cannot tell

Your answer must be exactly YES, NO, or UNCERTAIN."""

_RELEVANCE_RETRY_PROMPT = """You are deciding whether a news article reports on {company} directly experiencing the topic "{topic}".

Company: {company}
Topic: {topic}
Article title: {title}
Article content: {content}

You previously said you were UNCERTAIN. Think step by step:
1. Does the headline name {company} as the actor of "{topic}"?
2. If the article is a multi-topic news digest, is {company} the one doing "{topic}",
   or is it some other company in a different section of the digest?
3. Is {company} the agent of the topic event, or merely mentioned in passing?

Final answer with exactly one word:
- YES: The article reports on {company}'s own "{topic}"
- NO: It does not (either different company or only a passing mention of {company})

Your answer must be exactly YES or NO."""

_FACT_CHECK_PROMPT = """You are evaluating whether a news article contains hard facts or opinions/speculation.

Article title: {title}
Article content: {content}

Is this article reporting verifiable facts or expressing opinions/speculation? Answer with exactly one word:
- HARD_FACT: The article reports concrete, verifiable events (e.g., announced layoffs, confirmed funding, published breach disclosure). If the article reports a concrete event AND also includes analyst commentary or opinion, it is still HARD_FACT — the presence of commentary does not negate the factual event being reported.
- OPINION: The article is PURELY analysis, prediction, speculation, or opinion with NO concrete event being reported (e.g., "analysts believe layoffs may come", "might happen", "could potentially", "rumored to be considering").

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
    # Use the best human-readable company name for the validation prompt.
    # ctx.company is the raw domain (e.g., "meta.com") but news articles
    # refer to "Meta" or "Meta Platforms".  The LLM rejects articles when
    # asked "is this about meta.com?" because the text says "Meta".
    # Prefer the longest multi-word name (e.g., "meta platforms") for
    # highest match rate, fall back to first resolved name, then domain.
    if ctx.company_names:
        spaced = [n for n in ctx.company_names if " " in n]
        company = spaced[0] if spaced else ctx.company_names[0]
    else:
        company = ctx.company.strip().lower() if ctx.company else ""
    # Use the simplified topic when present; fall back to the original.
    topic_for_prompt = ""
    if ctx.topic is not None:
        topic_for_prompt = (
            (ctx.topic.simplified or ctx.topic.original or "").strip().lower()
        )
    if not topic_for_prompt and ctx.topics:
        topic_for_prompt = ctx.topics[-1].strip().lower()
    title = candidate.get("title", "")
    content = candidate.get("content", "")
    url = candidate.get("url", "")
    item_id = url or candidate.get("title", "")[:50]

    model = ctx.policy.model_for_task("validation")

    # ── Step 1: Relevance check ──
    relevance_prompt = _RELEVANCE_PROMPT.format(
        company=company, topic=topic_for_prompt, title=title, content=content[:2000]
    )
    relevance_text, relevance_usage = await llm.call(
        model=model, max_tokens=32, prompt=relevance_prompt
    )
    relevance = _parse_relevance(relevance_text)

    total_usage = dict(relevance_usage)

    # Retry on UNCERTAIN
    if relevance == "UNCERTAIN":
        retry_prompt = _RELEVANCE_RETRY_PROMPT.format(
            company=company,
            topic=topic_for_prompt,
            title=title,
            content=content[:2000],
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
            output={
                "result": None,
                "status": "not_about_company",
                "original": candidate,
            },
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
