"""Player prop model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlayerProp:
    prop_id: str
    card_date: str
    player_name: str
    team_name: str
    opponent_name: str
    prop_type: str
    confidence_grade: str
    status: str = "pending"
