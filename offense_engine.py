"""Offense scoring for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def offense_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score offensive separation using verified team/batter fields."""
    savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
    fg = game.get("fangraphs") if isinstance(game.get("fangraphs"), dict) else {}
    available = 0
    side_scores = {}
    for side in ("away", "home"):
        team = savant.get(f"{side}_team") if isinstance(savant.get(f"{side}_team"), dict) else {}
        batting = fg.get(f"{side}_team_batting") if isinstance(fg.get(f"{side}_team_batting"), dict) else {}
        xwoba = _num(team.get("xwOBA") or batting.get("wOBA"), 0.310)
        ops = _num(team.get("OPS") or batting.get("OPS"), 0.700)
        hard = _num(team.get("Hard Hit %") or batting.get("Hard%"), 38)
        barrel = _num(team.get("Barrel %"), 7)
        available += sum(value not in (0, 0.0) for value in (xwoba, ops, hard, barrel))
        score = 50 + (xwoba - 0.310) * 180 + (ops - 0.700) * 45 + (hard - 38) * 0.45 + (barrel - 7) * 1.2
        side_scores[side] = clamp(score)
    edge = abs(side_scores.get("home", 50) - side_scores.get("away", 50))
    return clamp(55 + edge), {"side_scores": side_scores, "available_fields": available, "possible_fields": 8}
