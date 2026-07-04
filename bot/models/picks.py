"""Pick model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Pick:
    pick_id: str
    card_date: str
    sport: str
    market_type: str
    pick_text: str
    status: str = "pending"
    result: str | None = None
    game_pk: int | None = None
    selected_team: str | None = None
    line: float | None = None
    metadata: dict[str, Any] | None = None
