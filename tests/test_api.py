"""API contract tests — verify endpoints, validation, response shapes.

- Pure tests: validation errors, response shape, health check (no infra)
- Real API tests: end-to-end /research calls (skipped without keys)
"""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from api import (
    ABSOLUTE_MAX_COST,
    Answer,
    AsyncResearchRequest,
    AsyncResearchResponse,
    Budget,
    Cost,
    Event,
    ResearchRequest,
    ResearchResponse,
    Signal,
    TopicResults,
    Usage,
    ValidSource,
    _background_tasks,
    _post_webhook,
    _track_task,
    app,
    lifespan,
)

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

@pytest.fixture
def client():
    """Synchronous TestClient for validation tests."""
    with patch("api.llm.init", new_callable=AsyncMock), \
         patch("api.search.init", new_callable=AsyncMock), \
         patch("api.scrape.init", new_callable=AsyncMock), \
         patch("api.llm.close", new_callable=AsyncMock), \
         patch("api.search.close", new_callable=AsyncMock), \
         patch("api.scrape.close", new_callable=AsyncMock):
        with TestClient(app) as c:
            yield c


@pytest.fixture(autouse=True)
def reset_background_tasks():
    """Clear background tasks between tests."""
    _background_tasks.clear()
    yield
    _background_tasks.clear()


# ═══════════════════════════════════════════════
# Pure tests — no infrastructure needed
# ═══════════════════════════════════════════════

class TestHealthCheck:
    """Health check endpoint tests."""

    def test_health_returns_ok(self, client):
        """GET / returns status ok."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "Revgent API"


class TestRequestValidation:
    """Pydantic validation rejects bad requests."""

    def test_missing_company_rejected(self, client):
        """Missing company domain returns 422."""
        response = client.post("/research", json={"topics": ["layoffs"]})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("company" in str(e).lower() for e in detail)

    def test_empty_company_rejected(self, client):
        """Empty company domain returns 422."""
        response = client.post("/research", json={"company": "", "topics": ["layoffs"]})
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("company" in str(e).lower() for e in detail)

    def test_invalid_depth_rejected(self, client):
        """Bad depth value returns 422."""
        response = client.post("/research", json={
            "company": "meta.com",
            "topics": ["layoffs"],
            "depth": "ultra",
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("depth" in str(e).lower() for e in detail)

    def test_budget_exceeds_absolute_max_rejected(self, client):
        """Budget above absolute max returns 422."""
        response = client.post("/research", json={
            "company": "meta.com",
            "topics": ["layoffs"],
            "max_cost": ABSOLUTE_MAX_COST + 1.0,
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("max_cost" in str(e).lower() for e in detail)

    def test_valid_request_accepted(self, client):
        """Valid request passes validation (may fail at pipeline)."""
        response = client.post("/research", json={
            "company": "meta.com",
            "topics": ["layoffs"],
            "depth": "cheap",
        })
        # Should not be a validation error
        assert response.status_code != 422


class TestAsyncRequestValidation:
    """Async endpoint validation."""

    def test_missing_webhook_rejected(self, client):
        """Missing webhook_url returns 422."""
        response = client.post("/research/async", json={
            "company": "meta.com",
            "topics": ["layoffs"],
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("webhook_url" in str(e).lower() for e in detail)

    def test_invalid_webhook_rejected(self, client):
        """Non-HTTP webhook_url returns 422."""
        response = client.post("/research/async", json={
            "company": "meta.com",
            "topics": ["layoffs"],
            "webhook_url": "ftp://example.com/callback",
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert any("webhook_url" in str(e).lower() for e in detail)

    def test_async_returns_processing_immediately(self, client):
        """Async endpoint returns processing status immediately."""
        with patch("api.run") as mock_run:
            mock_run.return_value = {"company": "meta.com", "events": []}
            response = client.post("/research/async", json={
                "company": "meta.com",
                "topics": ["layoffs"],
                "webhook_url": "https://example.com/webhook",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "processing"
            assert data["request_id"]
            assert "meta.com" in data["message"]


class TestResponseShape:
    """Response dict matches v2 ResearchResponse field-for-field."""

    def test_research_response_fields(self):
        """ResearchResponse has all required v2 fields."""
        fields = set(ResearchResponse.model_fields.keys())
        expected = {
            "company", "events", "answers", "signals",
            "usage", "topic_results", "cost", "budget",
        }
        assert expected <= fields

    def test_event_fields(self):
        """Event model has all required v2 fields."""
        fields = set(Event.model_fields.keys())
        expected = {
            "headline", "description", "topic", "date",
            "source_name", "source_url", "content_type",
            "headline_has_numbers", "cost_attribution",
        }
        assert expected <= fields

    def test_signal_fields(self):
        """Signal model has all required v2 fields."""
        fields = set(Signal.model_fields.keys())
        expected = {
            "headline", "description", "topic", "date",
            "source_name", "source_url", "signal_type",
            "confidence", "why_not_event", "cost_attribution",
        }
        assert expected <= fields

    def test_answer_fields(self):
        """Answer model has all required v2 fields."""
        fields = set(Answer.model_fields.keys())
        expected = {
            "topic", "validity", "confirmation", "timing",
            "summary", "valid_sources",
        }
        assert expected <= fields

    def test_valid_source_fields(self):
        """ValidSource model has all required v2 fields."""
        fields = set(ValidSource.model_fields.keys())
        expected = {
            "title", "source_name", "source_url",
            "published_date", "supports_claim",
        }
        assert expected <= fields

    def test_usage_fields(self):
        """Usage model has all required v2 fields."""
        fields = set(Usage.model_fields.keys())
        expected = {"input_tokens", "output_tokens", "total_tokens"}
        assert expected <= fields

    def test_topic_results_fields(self):
        """TopicResults model has all required v2 fields."""
        fields = set(TopicResults.model_fields.keys())
        expected = {"topic_found", "topic_count", "topic_name"}
        assert expected <= fields

    def test_cost_fields(self):
        """Cost model has all required v2 fields."""
        fields = set(Cost.model_fields.keys())
        expected = {"total_cost", "budget", "budget_exhausted", "breakdown"}
        assert expected <= fields

    def test_budget_fields(self):
        """Budget model has all required v2 fields."""
        fields = set(Budget.model_fields.keys())
        expected = {"requested", "remaining", "exhausted"}
        assert expected <= fields

    def test_research_request_fields(self):
        """ResearchRequest has expected fields."""
        fields = set(ResearchRequest.model_fields.keys())
        expected = {"company", "topics", "depth", "max_cost", "date_min", "date_max"}
        assert expected <= fields

    def test_async_request_fields(self):
        """AsyncResearchRequest has expected fields."""
        fields = set(AsyncResearchRequest.model_fields.keys())
        expected = {"company", "topics", "depth", "max_cost", "date_min", "date_max", "webhook_url"}
        assert expected <= fields

    def test_async_response_fields(self):
        """AsyncResearchResponse has expected fields."""
        fields = set(AsyncResearchResponse.model_fields.keys())
        expected = {"status", "request_id", "message"}
        assert expected <= fields


class TestBackgroundTasks:
    """Background task tracking for graceful shutdown."""

    async def test_track_task_adds_to_set(self):
        """_track_task adds task to tracked set."""
        async def dummy():
            pass

        task = asyncio.create_task(dummy())
        _track_task(task)
        assert task in _background_tasks
        await task

    async def test_task_removed_on_completion(self):
        """Completed tasks are removed from tracked set."""
        async def dummy():
            pass

        task = asyncio.create_task(dummy())
        _track_task(task)
        await task
        await asyncio.sleep(0.01)  # Let callback fire
        assert task not in _background_tasks

    async def test_post_webhook_best_effort(self):
        """_post_webhook does not raise on failure."""
        # This should not raise even for invalid URL
        await _post_webhook("http://localhost:99999/invalid", {"test": True})


class TestLifespan:
    """Provider lifecycle via lifespan."""

    async def test_lifespan_calls_init_and_close(self):
        """Lifespan calls provider init() on enter and close() on exit."""
        with patch("api.llm.init", new_callable=AsyncMock) as mock_llm_init, \
             patch("api.search.init", new_callable=AsyncMock) as mock_search_init, \
             patch("api.scrape.init", new_callable=AsyncMock) as mock_scrape_init, \
             patch("api.llm.close", new_callable=AsyncMock) as mock_llm_close, \
             patch("api.search.close", new_callable=AsyncMock) as mock_search_close, \
             patch("api.scrape.close", new_callable=AsyncMock) as mock_scrape_close:

            async with lifespan(app):
                mock_llm_init.assert_called_once()
                mock_search_init.assert_called_once()
                mock_scrape_init.assert_called_once()

            mock_llm_close.assert_called_once()
            mock_search_close.assert_called_once()
            mock_scrape_close.assert_called_once()

    async def test_lifespan_awaits_background_tasks(self):
        """Lifespan exit cancels and awaits background tasks."""
        async def slow_task():
            await asyncio.sleep(100)

        task = asyncio.create_task(slow_task())
        _track_task(task)

        with patch("api.llm.init", new_callable=AsyncMock), \
             patch("api.search.init", new_callable=AsyncMock), \
             patch("api.scrape.init", new_callable=AsyncMock), \
             patch("api.llm.close", new_callable=AsyncMock), \
             patch("api.search.close", new_callable=AsyncMock), \
             patch("api.scrape.close", new_callable=AsyncMock):

            async with lifespan(app):
                pass

        # Task should be cancelled after lifespan exit
        assert task.cancelled() or task.done()


class TestDepthTimeouts:
    """Per-depth timeout configuration."""

    def test_cheap_timeout(self):
        """Cheap depth has 30s timeout."""
        from api import _DEPTH_TIMEOUTS
        assert _DEPTH_TIMEOUTS["cheap"] == 30.0

    def test_standard_timeout(self):
        """Standard depth has 60s timeout."""
        from api import _DEPTH_TIMEOUTS
        assert _DEPTH_TIMEOUTS["standard"] == 60.0

    def test_deep_timeout(self):
        """Deep depth has 120s timeout."""
        from api import _DEPTH_TIMEOUTS
        assert _DEPTH_TIMEOUTS["deep"] == 120.0


class TestErrorResponses:
    """Error responses have actionable context."""

    def test_validation_error_includes_field(self, client):
        """422 responses include the field that failed validation."""
        response = client.post("/research", json={
            "company": "meta.com",
            "depth": "invalid",
        })
        assert response.status_code == 422
        detail = response.json()["detail"]
        # FastAPI/Pydantic v2 detail is a list of error objects
        assert len(detail) > 0
        # Should mention depth
        assert any("depth" in str(e).lower() for e in detail)


# ═══════════════════════════════════════════════
# Real API tests — need running server or ASGI
# ═══════════════════════════════════════════════

@skip_if_no_key
@skip_if_no_searxng
class TestApiReal:
    """End-to-end API tests against real infrastructure."""

    async def test_research_cheap_returns_response(self):
        """POST /research with cheap depth returns valid response."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/research", json={
                "company": "meta.com",
                "topics": ["layoffs"],
                "depth": "cheap",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["company"] == "meta.com"
            assert "events" in data
            assert "answers" in data
            assert "signals" in data
            assert "usage" in data
            assert "cost" in data
            assert "budget" in data

    async def test_research_response_validates(self):
        """Response validates against ResearchResponse model."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/research", json={
                "company": "meta.com",
                "topics": ["layoffs"],
                "depth": "cheap",
            })
            assert response.status_code == 200
            data = response.json()
            # Should not raise validation error
            ResearchResponse.model_validate(data)

    async def test_research_budget_not_exceeded(self):
        """Cheap research stays within budget."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/research", json={
                "company": "meta.com",
                "topics": ["layoffs"],
                "depth": "cheap",
            })
            assert response.status_code == 200
            data = response.json()
            assert data["cost"]["total_cost"] <= 0.015
            assert data["budget"]["requested"] == 0.01
