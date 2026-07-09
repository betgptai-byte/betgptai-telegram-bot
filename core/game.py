"""GameContext model — structured context about a single game on a slate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GameContext:
    game_pk: int | None = None
    away_team: str = ""
    home_team: str = ""
    game_time_et: str = ""
    status: str = "scheduled"
    sport: str | None = None
    league: str | None = None
    venue: str | None = None
    metadata: dict[str, Any] | None = None
