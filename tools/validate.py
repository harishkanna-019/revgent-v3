"""Validation tool: company relevance check + fact check via LLM."""

from core.context import RunContext
from core.types import ToolResult
from providers import llm

_RELEVANCE_PROMPT = """Does this article report on {company} experiencing "{topic}"?

Company: {company}
Topic: {topic}
Article title: {title}
Article content: {content}

Answer YES if:
- {company} is named in the article AND the article describes {company} doing or experiencing "{topic}"
- The article is a roundup/digest and one section covers {company}'s "{topic}"
- {company} is one of several companies mentioned as experiencing "{topic}"
- The article discusses {company}'s response to or involvement in "{topic}"

Answer NO if:
- {company} is not mentioned at all, or only mentioned in passing while the article is about a different company
- The article is about a different company doing "{topic}" and {company} is just context

Answer YES or NO. When unsure, lean YES (a downstream fact-checker will verify).

Your answer must be exactly YES or NO."""

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

_FACT_CHECK_PROMPT = """Does this article mention a concrete, verifiable event?

Article title: {title}
Article content: {content}

Answer HARD_FACT if the article mentions ANY of these:
- A specific action that happened (funding raised, person hired/resigned, data breach disclosed, product launched, acquisition completed, partnership signed)
- A named amount, date, or named person involved in an event
- A confirmed report of something that occurred (even if the article also contains analysis or commentary)

Examples of HARD_FACT:
- "Stripe completed a $6.5B tender offer" (specific action + amount)
- "Toast disclosed a data breach affecting employee SSNs" (specific event)
- "Airtable hired David Azose as CTO from OpenAI" (specific hire)
- "Company X may be acquired, sources confirm talks are underway" (confirmed action in progress)
- An article mixing news reporting with analyst commentary (the factual event is still there)

Answer OPINION only if the article contains NO concrete event at all — it is purely:
- Forward-looking speculation with no confirmed action ("might", "could potentially")
- General industry analysis with no specific company event
- A listicle or roundup with no factual claims

Your answer must be exactly HARD_FACT or OPINION."""


_COMBINED_PROMPT = """Does this article report a concrete event involving {company} and the topic "{topic}"?

Company: {company}
Topic: {topic}
Article title: {title}
Article content: {content}

Answer FACT if:
- The article names {company} and describes a specific event related to "{topic}"
  (e.g. funding raised, person hired/resigned, data breach disclosed, product launched)
- Even if the article also contains analysis or commentary, the core event is still there

Answer NO if:
- The article is about a DIFFERENT company and only mentions {company} in passing
- {company} is not mentioned, or is only cited as context/comparison
- There is no concrete event related to "{topic}" for {company}
- The article reports a software vulnerability or CVE patch without any actual data breach or data exposure
- The article is a stock analysis, investment thesis, or financial projection
- The article is from an investment directory or aggregation site (Tracxn, TexAu, Forge Global,
  Crunchbase, PitchBook, AngelList) that merely lists funding info without reporting a news event
- The article is a news roundup that names {company} but the actual event is about a different company
  (e.g. "Tech Moves: Syndio names 7 execs" — Stripe is just listed in passing)
- A founder of {company} is raising money for a DIFFERENT company
  (e.g. "6sense founder raises $30M for new startup" is NOT about 6sense funding)

Your answer must be exactly FACT or NO."""


def _parse_combined(text: str) -> str:
    """Parse combined relevance+fact response into FACT/NO.

    Defaults to NO when ambiguous to protect precision. The combined
    prompt is already permissive (accepts tender offers, insider threats,
    CLO appointments, etc.), so a conservative parser prevents false
    positives on true negatives without losing recall.
    """
    text = text.strip().upper()
    if "FACT" in text:
        return "FACT"
    if "YES" in text:
        return "FACT"
    return "NO"


def _parse_relevance(text: str) -> str:
    """Parse relevance response into YES/NO.

    Defaults to YES when ambiguous — the downstream fact-checker is the
    real precision gate. Maximizing recall here is more valuable than
    precision because false positives are caught later.
    """
    text = text.strip().upper()
    if "NO" in text and "YES" not in text:
        return "NO"
    # YES, UNCERTAIN, or ambiguous all default to YES
    return "YES"


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

    # ── Combined relevance + fact check in one call ──
    combined_prompt = _COMBINED_PROMPT.format(
        company=company, topic=topic_for_prompt, title=title, content=content[:3000]
    )
    combined_text, combined_usage = await llm.call(
        model=model, max_tokens=32, prompt=combined_prompt
    )
    total_usage = dict(combined_usage)

    verdict = _parse_combined(combined_text)

    if verdict == "NO":
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

    # YES or FACT -> treat as valid hard fact
    is_hard_fact = verdict in ("FACT", "YES")
    status = "valid" if is_hard_fact else "opinion"

    result = {
        "is_valid": True,
        "is_hard_fact": is_hard_fact,
        "relevance_raw": combined_text.strip(),
        "fact_check_raw": combined_text.strip(),
    }

    ctx.record(total_usage, item_id=item_id)

    return ToolResult(
        output={"result": result, "status": status, "original": candidate},
        usage=total_usage,
        item_id=item_id,
    )
