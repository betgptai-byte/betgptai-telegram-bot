"""StructuredCard model — container for official picks and card metadata.

The StructuredCard is the top-level output of card generation.  It replaces
the free-form Telegram text as the artifact that gets persisted and posted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.pick import OfficialPick, official_pick_to_dict


@dataclass
class StructuredCard:
    card_date: str
    display_date: str
    sport: str = "mlb"
    league: str = "MLB"
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    official_picks: list[OfficialPick] = field(default_factory=list)
    display_sections: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


def structured_card_to_dict(card: StructuredCard) -> dict[str, Any]:
    """Serialize a StructuredCard to a plain dict for JSON persistence."""
    return {
        "card_date": card.card_date,
        "display_date": card.display_date,
        "sport": card.sport,
        "league": card.league,
        "generated_at": card.generated_at,
        "official_picks": [official_pick_to_dict(p) for p in card.official_picks],
        "display_sections": card.display_sections,
        "metadata": card.metadata,
    }
