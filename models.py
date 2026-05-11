"""Data models for token tracking, cost attribution, and signal classification."""

from dataclasses import dataclass, field


@dataclass
class UsageStats:
    """Accumulates token usage across all LLM calls in a request."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def add(self, usage: dict) -> None:
        """Add a usage dict to the accumulator."""
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)

    def to_dict(self) -> dict:
        """Return the standard v2 usage shape."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class CostTracker:
    """Tracks USD cost with budget enforcement and per-item attribution.

    - Shared costs (topic analysis, query generation) are amortized across
      all items that benefit from them.
    - Direct costs (validation, formatting per URL) are tagged per item.
    """

    total_cost: float = 0.0
    budget: float = 0.0
    breakdown: dict[str, float] = field(default_factory=lambda: {"llm": 0.0, "search": 0.0, "scrape": 0.0})
    # item_id -> accumulated direct cost for that item
    per_item: dict[str, float] = field(default_factory=dict)
    # Shared costs to amortize (e.g., topic analysis, query generation)
    _shared_pending: list[tuple[float, int]] = field(default_factory=list, repr=False)

    @property
    def is_exhausted(self) -> bool:
        """True when total cost has reached or exceeded the budget."""
        return self.total_cost >= self.budget

    def record(self, cost: float, item_id: str | None = None, category: str = "llm") -> None:
        """Record a cost.

        Args:
            cost: USD cost of this operation
            item_id: If given, cost is attributed directly to this item.
                     If None, cost is tracked as shared pending amortization.
            category: Cost category for the breakdown ("llm", "search", "scrape")
        """
        self.total_cost += cost
        self.breakdown[category] = self.breakdown.get(category, 0.0) + cost

        if item_id is not None:
            self.per_item[item_id] = self.per_item.get(item_id, 0.0) + cost
        else:
            # Shared cost: will be amortized when items are finalized
            self._shared_pending.append((cost, 1))

    def amortize_shared(self, item_ids: list[str]) -> dict[str, float]:
        """Amortize all pending shared costs across the given items.

        Returns a mapping of item_id -> amortized cost for this batch.
        """
        amortized: dict[str, float] = {}
        if not item_ids or not self._shared_pending:
            return amortized

        for cost, _ in self._shared_pending:
            per_item_share = cost / len(item_ids)
            for item_id in item_ids:
                amortized[item_id] = amortized.get(item_id, 0.0) + per_item_share

        # Clear pending after amortization
        self._shared_pending.clear()
        return amortized

    def cost_for_item(self, item_id: str, amortized: dict[str, float] | None = None) -> float:
        """Total cost attributed to an item (direct + amortized shared)."""
        direct = self.per_item.get(item_id, 0.0)
        shared = amortized.get(item_id, 0.0) if amortized else 0.0
        return round(direct + shared, 8)

    def to_dict(self) -> dict:
        """Return the standard v2 cost shape."""
        return {
            "total_cost": round(self.total_cost, 8),
            "budget": self.budget,
            "budget_exhausted": self.is_exhausted,
            "breakdown": {k: round(v, 8) for k, v in self.breakdown.items()},
        }


@dataclass
class ResearchSignal:
    """A soft-intelligence signal (opinion, speculation, early report)."""

    headline: str = ""
    description: str = ""
    topic: str = ""
    date: str = ""
    source_name: str = ""
    source_url: str = ""
    signal_type: str = ""  # market_speculation | unconfirmed | early_report | analyst_commentary
    confidence: float = 0.0
    why_not_event: str = ""
    cost_attribution: float = 0.0

    def to_dict(self) -> dict:
        return {
            "headline": self.headline,
            "description": self.description,
            "topic": self.topic,
            "date": self.date,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "signal_type": self.signal_type,
            "confidence": self.confidence,
            "why_not_event": self.why_not_event,
            "cost_attribution": self.cost_attribution,
        }
