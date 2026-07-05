"""Bullpen scoring for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def _num(value: Any, default: float) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def bullpen_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score bullpen separation from verified bullpen fields."""
    savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
    available = 0
    side_scores = {}
    for side in ("away", "home"):
        pen = savant.get(f"{side}_bullpen") if isinstance(savant.get(f"{side}_bullpen"), dict) else {}
        era = _num(pen.get("ERA"), 4.25)
        whip = _num(pen.get("WHIP"), 1.35)
        kbb = _num(pen.get("K-BB%"), 12)
        hard = _num(pen.get("Hard Hit %"), 39)
        available += sum(key in pen for key in ("ERA", "WHIP", "K-BB%", "Hard Hit %"))
        score = 55 + (4.25 - era) * 5 + (1.35 - whip) * 20 + (kbb - 12) * 0.7 + (39 - hard) * 0.45
        side_scores[side] = clamp(score)
    return clamp(55 + abs(side_scores.get("home", 50) - side_scores.get("away", 50))), {
        "side_scores": side_scores,
        "available_fields": available,
        "possible_fields": 8,
    }
