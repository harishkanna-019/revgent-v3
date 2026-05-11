"""Per-request mutable state with __slots__ for memory efficiency."""

from dataclasses import dataclass, field

from .depth import ResearchDepthPolicy
from models import CostTracker, UsageStats


@dataclass
class TopicState:
    """Mutable state for the current topic being researched."""

    original: str = ""
    simplified: str = ""
    keywords: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)


class RunContext:
    """Per-request mutable state. Never shared across requests.

    Immutable fields (set at creation):
        policy, company, topics, date_min, date_max

    Mutable accumulators:
        cost, usage, events, signals, topic
    """

    __slots__ = (
        "policy",
        "company",
        "topics",
        "date_min",
        "date_max",
        "cost",
        "usage",
        "events",
        "signals",
        "topic",
    )

    def __init__(
        self,
        policy: ResearchDepthPolicy,
        company: str,
        topics: list[str],
        date_min: int,
        date_max: int,
    ):
        self.policy = policy
        self.company = company
        self.topics = topics
        self.date_min = date_min
        self.date_max = date_max

        # Mutable accumulators
        self.cost = CostTracker(budget=policy.default_budget)
        self.usage = UsageStats()
        self.events: list[dict] = []
        self.signals: list[dict] = []
        self.topic: TopicState | None = None

    @property
    def exhausted(self) -> bool:
        """True when the budget is exhausted."""
        return self.cost.is_exhausted

    def record(self, usage: dict, item_id: str | None = None) -> None:
        """Record token usage and cost for an LLM call.

        Args:
            usage: Dict with input_tokens, output_tokens, total_tokens
            item_id: Optional item identifier for direct cost attribution
        """
        self.usage.add(usage)
        # Convert tokens to approximate cost (OpenRouter pricing varies by model)
        # Using a rough estimate: $1.50/M input tokens, $6.00/M output tokens
        # This is updated by the caller with actual model pricing if needed
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        estimated_cost = (input_tokens * 1.50 + output_tokens * 6.00) / 1_000_000
        self.cost.record(estimated_cost, item_id=item_id, category="llm")

    def build_response(self, topic_name: str) -> dict:
        """Build a partial or full response dict matching the v2 shape.

        Returns dict with:
            company, events, answers, signals, usage, topic_results, cost, budget
        """
        from answer_builder import build_answers

        answers = build_answers(self.events, self.topics)
        topic_results = {
            "topic_found": len(self.events) > 0,
            "topic_count": len(self.events),
            "topic_name": topic_name,
        }

        return {
            "company": self.company,
            "events": self.events,
            "answers": answers,
            "signals": self.signals,
            "usage": self.usage.to_dict(),
            "topic_results": topic_results,
            "cost": self.cost.to_dict(),
            "budget": {
                "requested": self.cost.budget,
                "remaining": round(max(0.0, self.cost.budget - self.cost.total_cost), 8),
                "exhausted": self.cost.is_exhausted,
            },
        }
