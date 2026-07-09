"""Matchup and situational scoring for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def situational_score(game: dict) -> tuple[float, dict]:
    """Score simple verified situational factors."""
    score = 55 if game.get("home_team") else 50
    if game.get("status") and "postponed" in str(game.get("status")).lower():
        score -= 20
    return clamp(score), {"available_fields": 1 if game.get("home_team") else 0, "possible_fields": 3}


def sp_batter_matchup_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score SP vs Batter matchup quality 0–100 for one game side.

    Runs the SP Batter Matchup Engine and returns an aggregate score
    based on team contact/power advantage and data quality.
    """
    from services.sp_batter_matchup_engine import build_sp_batter_matchups

    result = build_sp_batter_matchups(game)
    away = result.get("away_vs_home_sp") or {}
    home = result.get("home_vs_away_sp") or {}
    gl = result.get("game_level") or {}

    score = 50.0
    fields = 0
    possible = 14

    contact = gl.get("combined_contact_advantage", 50)
    power = gl.get("combined_power_advantage", 50)
    score += (contact - 50) * 0.3
    score += (power - 50) * 0.3
    fields += 2

    for side in (away, home):
        q = side.get("hitters_qualified", 0)
        scanned = side.get("hitters_scanned", 0)
        if scanned > 0:
            ratio = q / scanned
            score += ratio * 5
            fields += 1
            possible += 5

    return clamp(score), {
        "available_fields": fields,
        "possible_fields": possible,
        "combined_contact": contact,
        "combined_power": power,
        "hitters_qualified": away.get("hitters_qualified", 0) + home.get("hitters_qualified", 0),
        "hitters_scanned": away.get("hitters_scanned", 0) + home.get("hitters_scanned", 0),
        "data_quality_grade": gl.get("data_quality_grade", "C"),
    }


def matchup_summary(game: dict, scores: dict) -> dict:
    """Create an auditable matchup summary."""
    return {
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "matchup": f"{game.get('away_team')} @ {game.get('home_team')}",
        "scores": scores,
    }
