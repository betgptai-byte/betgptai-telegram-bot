"""Weather and park scoring for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def _num(value: Any, default: float) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def weather_park_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score run environment from verified weather and ballpark labels."""
    weather = game.get("weather") if isinstance(game.get("weather"), dict) else {}
    park = str(game.get("park_factor") or game.get("park_factor_label") or "").lower()
    temp = _num(weather.get("temperature"), 70)
    wind = _num(weather.get("wind_speed"), 5)
    score = 50
    if any(word in park for word in ("extreme hitter", "hitter", "hr-friendly")):
        score += 15
    if "pitcher" in park:
        score -= 10
    score += clamp((temp - 70) * 0.5, -8, 12)
    score += clamp(wind * 0.4, 0, 8)
    available = int(bool(weather)) + int(bool(park))
    return clamp(score), {"available_fields": available, "possible_fields": 2, "park_label": park or "unavailable"}
