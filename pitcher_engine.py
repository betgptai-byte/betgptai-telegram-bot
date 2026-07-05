"""Starting pitcher scoring for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def _num(value: Any, default: float | None = None) -> float | None:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def _pitcher_stats(game: dict[str, Any], side: str) -> dict[str, Any]:
    savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
    stats = game.get(f"{side}_pitcher_stats") if isinstance(game.get(f"{side}_pitcher_stats"), dict) else {}
    pitcher = savant.get(f"{side}_pitcher") if isinstance(savant.get(f"{side}_pitcher"), dict) else {}
    return {**stats, **pitcher}


def pitcher_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Calculate starting pitcher edge score for the stronger side."""
    side_scores: dict[str, float] = {}
    available = 0
    for side in ("away", "home"):
        data = _pitcher_stats(game, side)
        era = _num(data.get("ERA") or data.get("era") or data.get("xERA"), 4.50)
        whip = _num(data.get("WHIP") or data.get("whip"), 1.35)
        whiff = _num(data.get("Whiff %") or data.get("Whiff%"), 22.0)
        chase = _num(data.get("Chase %") or data.get("Chase%"), 28.0)
        hard_hit = _num(data.get("Hard Hit %") or data.get("HardHit%"), 40.0)
        barrel = _num(data.get("Barrel %") or data.get("Barrel%"), 8.0)
        available += sum(value is not None for value in (era, whip, whiff, chase, hard_hit, barrel))
        score = 55
        score += clamp((4.50 - (era or 4.50)) * 8, -20, 25)
        score += clamp((1.35 - (whip or 1.35)) * 25, -15, 20)
        score += clamp(((whiff or 22) - 22) * 0.8, -8, 15)
        score += clamp(((chase or 28) - 28) * 0.5, -5, 10)
        score += clamp((40 - (hard_hit or 40)) * 0.5, -8, 10)
        score += clamp((8 - (barrel or 8)) * 1.0, -8, 10)
        side_scores[side] = clamp(score)
    edge = abs(side_scores.get("home", 50) - side_scores.get("away", 50))
    return clamp(55 + edge), {"side_scores": side_scores, "available_fields": available, "possible_fields": 12}
