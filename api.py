"""Revgent v3 API — FastAPI transport layer.

All handlers are async def. Provider lifecycle managed via lifespan.
Background webhook tasks tracked for graceful shutdown.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from core.context import RunContext
from core.depth import ResearchDepthPolicy
from core.pipeline import run
from providers import llm, search, scrape


# ── Configuration ──

ABSOLUTE_MAX_COST = float(os.environ.get("ABSOLUTE_MAX_COST", "5.0"))

_DEPTH_TIMEOUTS = {
    "cheap": 30.0,
    "standard": 60.0,
    "deep": 120.0,
}


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
    """Synchronous research request."""
    company: str = Field(..., description="Company domain (e.g., meta.com)")
    topics: list[str] = Field(default_factory=list, description="List of topics to research")
    depth: str = Field(default="cheap", description="Research depth: cheap, standard, or deep")
    max_cost: float | None = Field(default=None, description="Maximum budget in USD (capped at absolute max)")
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
    """Asynchronous research request with webhook callback."""
    company: str = Field(..., description="Company domain (e.g., meta.com)")
    topics: list[str] = Field(default_factory=list, description="List of topics to research")
    depth: str = Field(default="cheap", description="Research depth: cheap, standard, or deep")
    max_cost: float | None = Field(default=None, description="Maximum budget in USD (capped at absolute max)")
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
async def lifespan(app: FastAPI):
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
    """Health check endpoint."""
    return {"status": "ok", "service": "Revgent API"}


@app.post("/research", response_model=ResearchResponse)
async def research(req: ResearchRequest) -> dict[str, Any]:
    """Run synchronous research and return full results."""
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


@app.post("/research/async", response_model=AsyncResearchResponse)
async def research_async(req: AsyncResearchRequest) -> AsyncResearchResponse:
    """Start asynchronous research and return immediately.

    Results are POSTed to the webhook_url when complete.
    """
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
