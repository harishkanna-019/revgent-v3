"""Shared type definitions. Frozen dataclasses, no runtime code."""

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolResult:
    """Result returned by every tool."""

    output: Any = None
    usage: dict = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    )
    item_id: str | None = None


@dataclass(frozen=True)
class StageStart:
    """Emitted when a pipeline stage begins."""

    type: str = "stage_start"
    stage: str = ""
    count: int = 0


@dataclass(frozen=True)
class StageEnd:
    """Emitted when a pipeline stage ends."""

    type: str = "stage_end"
    stage: str = ""
    out: int = 0


@dataclass(frozen=True)
class ItemResult:
    """Emitted for each item processed in a parallel stage."""

    type: str = "item_result"
    stage: str = ""
    item_id: str = ""
    status: str = ""


@dataclass(frozen=True)
class BudgetCheck:
    """Emitted after each budget check."""

    type: str = "budget"
    spent: float = 0.0
    remaining: float = 0.0


Event = StageStart | StageEnd | ItemResult | BudgetCheck
Emit = Callable[[Event], None] | None
