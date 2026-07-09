"""BETGPTAI Core — structured card architecture.

Phase 1: core data models without changing public behavior.
"""
from __future__ import annotations

from core.pick import OfficialPick, official_pick_to_dict
from core.card import StructuredCard, structured_card_to_dict
from core.game import GameContext
from core.market import MarketContext

__all__ = [
    "OfficialPick",
    "official_pick_to_dict",
    "StructuredCard",
    "structured_card_to_dict",
    "GameContext",
    "MarketContext",
]
