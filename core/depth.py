"""Research depth configuration. Frozen, immutable per-request."""

from dataclasses import dataclass, field


# Model pricing: (input_price_per_1M, output_price_per_1M) in USD
# See OpenRouter pricing page for current rates
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "deepseek/deepseek-v4-flash:nitro": (0.055, 0.11),
    "deepseek/deepseek-v4-pro:nitro": (0.89, 1.79),
    "moonshotai/kimi-k2.6:nitro": (2.00, 8.00),
}


@dataclass(frozen=True)
class ResearchDepthPolicy:
    """Immutable research depth configuration."""

    depth: str
    max_candidates_per_topic: int
    max_queries_per_topic: int
    max_extraction_chars: int
    max_full_extraction_candidates: int
    default_budget: float
    max_workers: int
    model_map: dict[str, str] = field(repr=False)

    def model_for_task(self, task: str) -> str:
        """Route a task name to the correct OpenRouter model identifier.

        Task names: topic_simplification, keyword_generation, query_generation,
        validation, fact_check, summarization, classification
        """
        return self.model_map.get(
            task, self.model_map.get("default", "deepseek/deepseek-v4-flash:nitro")
        )

    def model_cost(self, model: str) -> tuple[float, float]:
        """Return (input_price_per_M, output_price_per_M) for a model.

        Falls back to flash pricing if model is unknown.
        """
        return _MODEL_PRICING.get(model, (0.055, 0.11))

    @classmethod
    def from_request(
        cls, depth: str = "cheap", max_cost: float | None = None
    ) -> "ResearchDepthPolicy":
        """Create a ResearchDepthPolicy from a request depth string.

        Args:
            depth: One of "cheap", "standard", "deep"
            max_cost: Optional user-requested max cost (capped at absolute max)

        Returns:
            Frozen ResearchDepthPolicy instance
        """
        # Absolute max budget caps any user request
        absolute_max = 5.0

        # Depth profiles — field-for-field identical to v2
        profiles: dict[str, dict] = {
            "cheap": {
                "max_candidates_per_topic": 3,
                "max_queries_per_topic": 2,
                "max_extraction_chars": 0,
                "max_full_extraction_candidates": 0,
                "default_budget": 0.01,
                "max_workers": 3,
                "model_map": {
                    "default": "deepseek/deepseek-v4-flash:nitro",
                    "topic_simplification": "deepseek/deepseek-v4-flash:nitro",
                    "keyword_generation": "deepseek/deepseek-v4-flash:nitro",
                    "query_generation": "deepseek/deepseek-v4-flash:nitro",
                    "validation": "deepseek/deepseek-v4-flash:nitro",
                    "fact_check": "deepseek/deepseek-v4-flash:nitro",
                    "summarization": "deepseek/deepseek-v4-flash:nitro",
                    "classification": "deepseek/deepseek-v4-flash:nitro",
                },
            },
            "standard": {
                "max_candidates_per_topic": 8,
                "max_queries_per_topic": 5,
                "max_extraction_chars": 4000,
                "max_full_extraction_candidates": 6,
                "default_budget": 0.50,
                "max_workers": 8,
                "model_map": {
                    "default": "deepseek/deepseek-v4-flash:nitro",
                    "topic_simplification": "deepseek/deepseek-v4-flash:nitro",
                    "keyword_generation": "deepseek/deepseek-v4-flash:nitro",
                    "query_generation": "deepseek/deepseek-v4-flash:nitro",
                    "validation": "deepseek/deepseek-v4-flash:nitro",
                    "fact_check": "deepseek/deepseek-v4-flash:nitro",
                    "summarization": "deepseek/deepseek-v4-flash:nitro",
                    "classification": "deepseek/deepseek-v4-flash:nitro",
                },
            },
            "deep": {
                "max_candidates_per_topic": 20,
                "max_queries_per_topic": 12,
                "max_extraction_chars": 8000,
                "max_full_extraction_candidates": 20,
                "default_budget": 2.00,
                "max_workers": 16,
                "model_map": {
                    "default": "deepseek/deepseek-v4-pro:nitro",
                    "topic_simplification": "deepseek/deepseek-v4-pro:nitro",
                    "keyword_generation": "deepseek/deepseek-v4-pro:nitro",
                    "query_generation": "deepseek/deepseek-v4-pro:nitro",
                    "validation": "moonshotai/kimi-k2.6:nitro",
                    "fact_check": "moonshotai/kimi-k2.6:nitro",
                    "summarization": "moonshotai/kimi-k2.6:nitro",
                    "classification": "moonshotai/kimi-k2.6:nitro",
                },
            },
        }

        if depth not in profiles:
            raise ValueError(
                f"Unknown depth '{depth}'. Must be one of: cheap, standard, deep"
            )
        profile = profiles[depth]

        # Cap budget at absolute max
        budget = profile["default_budget"]
        if max_cost is not None:
            budget = min(max_cost, absolute_max)

        return cls(
            depth=depth,
            max_candidates_per_topic=profile["max_candidates_per_topic"],
            max_queries_per_topic=profile["max_queries_per_topic"],
            max_extraction_chars=profile["max_extraction_chars"],
            max_full_extraction_candidates=profile["max_full_extraction_candidates"],
            default_budget=budget,
            max_workers=profile["max_workers"],
            model_map=profile["model_map"],
        )
