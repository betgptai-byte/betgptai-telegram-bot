"""Defense scoring for BETGPTAI v20."""

from __future__ import annotations

from edge_database import clamp


def defense_score(game: dict) -> tuple[float, dict]:
    """Score defensive context when available.

    Current verified slate data rarely carries detailed defense metrics, so this
    defaults to neutral and reports limited data quality.
    """
    defense = game.get("defense") if isinstance(game.get("defense"), dict) else {}
    if not defense:
        return 50.0, {"available_fields": 0, "possible_fields": 2, "note": "neutral_no_verified_defense_feed"}
    return clamp(defense.get("score", 50)), {"available_fields": 1, "possible_fields": 2}
