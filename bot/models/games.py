"""Game model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Game:
    game_pk: int | None
    away_team: str
    home_team: str
    game_time_et: str
    status: str = "scheduled"
