"""MarketContext model — normalized odds context from any provider."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MarketContext:
    provider: str = ""
    moneyline: list[dict[str, Any]] = field(default_factory=list)
    runline: list[dict[str, Any]] = field(default_factory=list)
    total: list[dict[str, Any]] = field(default_factory=list)
    team_totals: list[dict[str, Any]] = field(default_factory=list)
    last_updated: str | None = None
    matched_game_pk: int | None = None
    matched_by: str = ""
    market_type: str = ""
    market_key: str = ""
    line: float | None = None
    odds: int | float | None = None
    outcome: str | None = None
    price: float | None = None
    point: float | None = None
