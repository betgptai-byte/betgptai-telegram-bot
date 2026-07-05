"""Matchup and situational scoring for BETGPTAI v20."""

from __future__ import annotations

from edge_database import clamp


def situational_score(game: dict) -> tuple[float, dict]:
    """Score simple verified situational factors."""
    score = 55 if game.get("home_team") else 50
    if game.get("status") and "postponed" in str(game.get("status")).lower():
        score -= 20
    return clamp(score), {"available_fields": 1 if game.get("home_team") else 0, "possible_fields": 3}


def matchup_summary(game: dict, scores: dict) -> dict:
    """Create an auditable matchup summary."""
    return {
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "scores": scores,
    }
