"""OfficialPick model — structured representation of a single bettor pick.

Never parse Telegram text to recover picks.  Use this model as the canonical
source of truth for every pick on a generated card.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any


@dataclass
class OfficialPick:
    pick_id: str
    sport: str
    league: str
    card_date: str
    game_pk: int | None = None
    game_time_et: str | None = None
    away_team: str | None = None
    home_team: str | None = None
    selected_team: str | None = None
    opponent: str | None = None
    market_type: str = "moneyline"
    market_line: float | None = None
    odds: int | float | None = None
    confidence: str | int | float | None = None
    edge_score: float | None = None
    risk_level: str | None = None
    data_quality_grade: str | None = None
    units: float = 1.0
    reason: str = ""
    status: str = "pending"
    result: str | None = None
    model_version: str = "BETGPTAI v20.0"


def official_pick_to_dict(pick: OfficialPick) -> dict[str, Any]:
    """Serialize an OfficialPick to a plain dict for JSON persistence."""
    result: dict[str, Any] = {}
    for f in fields(OfficialPick):
        value = getattr(pick, f.name)
        if value is not None:
            result[f.name] = value
        else:
            result[f.name] = None
    return result
