"""MarketContext model — structured context about a betting market line."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MarketContext:
    market_type: str = "moneyline"
    market_key: str = ""
    line: float | None = None
    odds: int | float | None = None
    outcome: str | None = None
    price: float | None = None
    point: float | None = None
