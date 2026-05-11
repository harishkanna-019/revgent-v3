"""Revgent v3 API — FastAPI transport layer.

All handlers are async def. Provider lifecycle managed via lifespan.
Background webhook tasks tracked for graceful shutdown.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from core.context import RunContext
from core.depth import ResearchDepthPolicy
from core.pipeline import run
from providers import llm, scrape, search

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("revgent.api")


# ── Configuration ──

ABSOLUTE_MAX_COST = float(os.environ.get("ABSOLUTE_MAX_COST", "5.0"))

# Per-depth wall-clock timeout. Scrape of 5 sites + 5 parallel validates +
# 5 parallel formats can easily eat 60s when news sites are slow. Generous
# numbers - the pipeline checks budget between stages and returns partial
# results if it runs out anyway.
_DEPTH_TIMEOUTS = {
    "cheap": 45.0,
    "standard": 120.0,
    "deep": 240.0,
}

# Optional shared-secret auth. If REVGENT_API_KEY is set, /research and
# /research/async require an `X-Api-Key` header matching it.
API_KEY = os.environ.get("REVGENT_API_KEY", "").strip()


def _check_auth(provided: str | None) -> None:
    if not API_KEY:
        return  # no auth configured
    if not provided or not hmac.compare_digest(provided.strip(), API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing X-Api-Key")


# ── Pydantic models (v2-identical) ──


class ValidSource(BaseModel):
    """A source that supports a claim."""

    title: str = ""
    source_name: str = ""
    source_url: str = ""
    published_date: str = ""
    supports_claim: bool = True


class Validity(BaseModel):
    """Validity assessment for a topic."""

    is_valid: bool = False
    statement: str = ""
    confidence: str = "low"


class Confirmation(BaseModel):
    """Confirmation details for a topic."""

    is_confirmed: bool = False
    statement: str = ""
    source_name: str = ""
    source_url: str = ""


class Timing(BaseModel):
    """Timing information for a topic."""

    happened_at: str = ""
    statement: str = ""


class Answer(BaseModel):
    """Per-topic answer object."""

    topic: str = ""
    validity: Validity = Field(default_factory=Validity)
    confirmation: Confirmation = Field(default_factory=Confirmation)
    timing: Timing = Field(default_factory=Timing)
    summary: str = ""
    valid_sources: list[ValidSource] = Field(default_factory=list)


class Event(BaseModel):
    """A validated hard-fact event."""

    headline: str = ""
    description: str = ""
    topic: str = ""
    date: str = ""
    source_name: str = ""
    source_url: str = ""
    content_type: str = "analysis"
    headline_has_numbers: bool = False
    cost_attribution: float = 0.0


class Signal(BaseModel):
    """A soft-intelligence signal."""

    headline: str = ""
    description: str = ""
    topic: str = ""
    date: str = ""
    source_name: str = ""
    source_url: str = ""
    signal_type: str = ""
    confidence: float = 0.0
    why_not_event: str = ""
    cost_attribution: float = 0.0


class Usage(BaseModel):
    """Token usage statistics."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class TopicResults(BaseModel):
    """Topic-level results summary."""

    topic_found: bool = False
    topic_count: int = 0
    topic_name: str = ""


class Cost(BaseModel):
    """Cost tracking and budget status."""

    total_cost: float = 0.0
    budget: float = 0.0
    budget_exhausted: bool = False
    breakdown: dict[str, float] = Field(default_factory=dict)


class Budget(BaseModel):
    """Budget information."""

    requested: float = 0.0
    remaining: float = 0.0
    exhausted: bool = False


class ResearchResponse(BaseModel):
    """Complete research response — identical to v2."""

    company: str = ""
    events: list[Event] = Field(default_factory=list)
    answers: list[Answer] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    topic_results: TopicResults = Field(default_factory=TopicResults)
    cost: Cost = Field(default_factory=Cost)
    budget: Budget = Field(default_factory=Budget)


class ResearchRequest(BaseModel):
    """Synchronous research request.

    Accepts either `company` (v3 native) or `company_domain` (v2 alias)
    so existing Clay integrations keep working.
    """

    model_config = {"populate_by_name": True}

    company: str = Field(
        ...,
        alias="company_domain",
        description="Company domain (e.g., meta.com). Accepts 'company' or 'company_domain'.",
    )
    topics: list[str] = Field(
        default_factory=list, description="List of topics to research"
    )
    depth: str = Field(
        default="cheap", description="Research depth: cheap, standard, or deep"
    )
    max_cost: float | None = Field(
        default=None, description="Maximum budget in USD (capped at absolute max)"
    )
    date_min: int = Field(default=0, description="Minimum days ago")
    date_max: int = Field(default=90, description="Maximum days ago")

    @field_validator("company")
    @classmethod
    def company_must_have_domain(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("company domain is required")
        return v

    @field_validator("depth")
    @classmethod
    def depth_must_be_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("cheap", "standard", "deep"):
            raise ValueError(f"depth must be one of: cheap, standard, deep (got '{v}')")
        return v

    @field_validator("max_cost")
    @classmethod
    def max_cost_within_limit(cls, v: float | None) -> float | None:
        if v is not None and v > ABSOLUTE_MAX_COST:
            raise ValueError(
                f"max_cost (${v}) exceeds absolute maximum (${ABSOLUTE_MAX_COST})"
            )
        return v


class AsyncResearchRequest(BaseModel):
    """Asynchronous research request with webhook callback.

    Accepts either `company` (v3 native) or `company_domain` (v2 alias).
    """

    model_config = {"populate_by_name": True}

    company: str = Field(
        ...,
        alias="company_domain",
        description="Company domain (e.g., meta.com). Accepts 'company' or 'company_domain'.",
    )
    topics: list[str] = Field(
        default_factory=list, description="List of topics to research"
    )
    depth: str = Field(
        default="cheap", description="Research depth: cheap, standard, or deep"
    )
    max_cost: float | None = Field(
        default=None, description="Maximum budget in USD (capped at absolute max)"
    )
    date_min: int = Field(default=0, description="Minimum days ago")
    date_max: int = Field(default=90, description="Maximum days ago")
    webhook_url: str = Field(..., description="URL to POST results to when complete")

    @field_validator("company")
    @classmethod
    def company_must_have_domain(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("company domain is required")
        return v

    @field_validator("depth")
    @classmethod
    def depth_must_be_valid(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("cheap", "standard", "deep"):
            raise ValueError(f"depth must be one of: cheap, standard, deep (got '{v}')")
        return v

    @field_validator("max_cost")
    @classmethod
    def max_cost_within_limit(cls, v: float | None) -> float | None:
        if v is not None and v > ABSOLUTE_MAX_COST:
            raise ValueError(
                f"max_cost (${v}) exceeds absolute maximum (${ABSOLUTE_MAX_COST})"
            )
        return v

    @field_validator("webhook_url")
    @classmethod
    def webhook_url_must_be_https(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("webhook_url must be a valid HTTP(S) URL")
        return v


class AsyncResearchResponse(BaseModel):
    """Immediate response for async research request."""

    status: str = "processing"
    request_id: str = ""
    message: str = ""


# ── Background task tracking ──

_background_tasks: set[asyncio.Task] = set()


def _track_task(task: asyncio.Task) -> None:
    """Add a task to the tracked set and schedule cleanup on done."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _post_webhook(url: str, payload: dict) -> None:
    """POST results to a webhook URL."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(url, json=payload)
    except Exception:
        # Webhook failures are best-effort; don't crash the server
        pass


# ── Lifespan ──


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize providers on startup, clean up on shutdown."""
    # Startup
    await llm.init()
    await search.init()
    await scrape.init()

    yield

    # Shutdown: cancel and await background tasks
    if _background_tasks:
        for task in list(_background_tasks):
            task.cancel()
        # Wait up to 10 seconds for tasks to finish
        await asyncio.wait(
            list(_background_tasks),
            timeout=10.0,
            return_when=asyncio.ALL_COMPLETED,
        )

    await scrape.close()
    await search.close()
    await llm.close()


# ── App ──

app = FastAPI(
    title="Revgent API",
    description="Async-first research agent for sales intelligence",
    version="3.0.0",
    lifespan=lifespan,
)


# ── Endpoints ──


@app.get("/")
async def health_check() -> dict[str, str]:
    """Health check endpoint. Always public."""
    return {"status": "ok", "service": "Revgent API"}


@app.post("/research", response_model=ResearchResponse)
async def research(
    req: ResearchRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> dict[str, Any]:
    """Run synchronous research and return full results."""
    _check_auth(x_api_key)
    policy = ResearchDepthPolicy.from_request(req.depth, max_cost=req.max_cost)
    ctx = RunContext(
        policy=policy,
        company=req.company,
        topics=req.topics,
        date_min=req.date_min,
        date_max=req.date_max,
    )

    timeout = _DEPTH_TIMEOUTS.get(req.depth, 30.0)
    return await run(ctx, timeout_seconds=timeout)


@app.post("/research/clay")
async def research_clay(
    req: ResearchRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> dict[str, Any]:
    """Clay-friendly endpoint that flattens the response for HTTP-column use.

    Returns the same data as /research but with top-level convenience fields
    so Clay can map columns directly without nested-path expressions.
    """
    _check_auth(x_api_key)
    request_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    logger.info(
        "clay request rid=%s company=%r topics=%r depth=%s max_cost=%s",
        request_id,
        req.company,
        req.topics,
        req.depth,
        req.max_cost,
    )
    # Track every pipeline stage so empty results are explainable.
    stage_trace: list[dict[str, Any]] = []

    def _trace(event: Any) -> None:
        # Capture only StageEnd events with their counts to keep this cheap.
        stage = getattr(event, "stage", None)
        if stage is None:
            return
        out = getattr(event, "out", None)
        if out is not None:
            stage_trace.append({"stage": stage, "out": out})

    policy = ResearchDepthPolicy.from_request(req.depth, max_cost=req.max_cost)
    ctx = RunContext(
        policy=policy,
        company=req.company,
        topics=req.topics,
        date_min=req.date_min,
        date_max=req.date_max,
    )
    timeout = _DEPTH_TIMEOUTS.get(req.depth, 30.0)
    try:
        full = await run(ctx, emit=_trace, timeout_seconds=timeout)
    except Exception as exc:
        logger.exception("clay request rid=%s failed: %s", request_id, exc)
        raise

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    events = full.get("events", [])
    signals = full.get("signals", [])
    answers = full.get("answers", [])
    # answer_builder already sorts events hard-fact-first, then by date.
    # answers[0].valid_sources[0] is the canonical 'best' source for this
    # response - mirror that here so primary_* fields stay consistent with
    # answers[0] (otherwise Clay sees an analysis piece as 'primary' while
    # the actual confirmation in answers points to a novel_fact).
    logger.info(
        "clay response rid=%s elapsed_ms=%d events=%d signals=%d cost=%.6f tokens=%d trace=%s",
        request_id,
        elapsed_ms,
        len(events),
        len(signals),
        full.get("cost", {}).get("total_cost", 0.0),
        full.get("usage", {}).get("total_tokens", 0),
        stage_trace,
    )
    primary_answer = answers[0] if answers else {}
    # Use the top valid_source from answers[0] when present - this is the
    # quality-sorted pick rather than first-by-pipeline-order.
    primary_sources = primary_answer.get("valid_sources") or []
    primary_source = primary_sources[0] if primary_sources else {}
    # Map the primary source back to its full event (by URL) for type info.
    primary_event: dict = {}
    if primary_source and events:
        url = primary_source.get("source_url", "")
        for e in events:
            if e.get("source_url") == url:
                primary_event = e
                break
    if not primary_event and events:
        primary_event = events[0]
    primary_signal = signals[0] if signals else {}

    return {
        # Top-level scalars Clay can map straight into columns
        "company": full.get("company", ""),
        "topic": req.topics[0] if req.topics else "",
        "event_count": len(events),
        "signal_count": len(signals),
        "is_valid": bool(primary_answer.get("validity", {}).get("is_valid", False)),
        "confidence": primary_answer.get("validity", {}).get("confidence", "low"),
        "summary": primary_answer.get("summary", ""),
        "primary_headline": primary_event.get("headline")
        or primary_signal.get("headline", ""),
        "primary_source_url": primary_event.get("source_url")
        or primary_signal.get("source_url", ""),
        "primary_source_name": primary_event.get("source_name")
        or primary_signal.get("source_name", ""),
        "primary_date": primary_event.get("date") or primary_signal.get("date", ""),
        "signal_type": primary_signal.get("signal_type", ""),
        "signal_confidence": float(primary_signal.get("confidence", 0.0)),
        "total_cost_usd": float(full.get("cost", {}).get("total_cost", 0.0)),
        "total_tokens": int(full.get("usage", {}).get("total_tokens", 0)),
        # Diagnostics for empty / unexpected results.
        "request_id": request_id,
        "elapsed_ms": elapsed_ms,
        "stage_trace": stage_trace,
        # Full payload still available if Clay wants to drill into arrays
        "events": events,
        "signals": signals,
        "answers": answers,
    }


@app.post("/research/async", response_model=AsyncResearchResponse)
async def research_async(
    req: AsyncResearchRequest,
    x_api_key: str | None = Header(default=None, alias="X-Api-Key"),
) -> AsyncResearchResponse:
    """Start asynchronous research and return immediately.

    Results are POSTed to the webhook_url when complete.
    """
    _check_auth(x_api_key)
    request_id = str(uuid.uuid4())

    async def _do_research() -> None:
        policy = ResearchDepthPolicy.from_request(req.depth, max_cost=req.max_cost)
        ctx = RunContext(
            policy=policy,
            company=req.company,
            topics=req.topics,
            date_min=req.date_min,
            date_max=req.date_max,
        )
        timeout = _DEPTH_TIMEOUTS.get(req.depth, 30.0)
        result = await run(ctx, timeout_seconds=timeout)
        await _post_webhook(req.webhook_url, result)

    task = asyncio.create_task(_do_research())
    _track_task(task)

    return AsyncResearchResponse(
        status="processing",
        request_id=request_id,
        message=f"Research started for {req.company}. Results will be sent to {req.webhook_url}",
    )
