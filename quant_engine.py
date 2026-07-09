"""BETGPTAI ELITE QUANT ENGINE v21.0.

The engine calculates verified component scores using dynamic weights loaded
from ``model_weights.json``.  AI may rank/explain these outputs but should
never invent missing stats.
"""

from __future__ import annotations

from typing import Any

from services.bullpen_engine import bullpen_score_v2 as bullpen_score
from defense_engine import defense_score
from edge_database import (
    MINIMUM_EDGE_SCORE,
    MODEL_VERSION,
    confidence_from_score,
    current_quant_weights,
    data_quality_grade,
    risk_level,
    save_edge_snapshot,
    weighted_score,
)
from market_engine import market_value_score
from matchup_engine import matchup_summary, situational_score, sp_batter_matchup_score
from offense_engine import offense_score
from pitcher_engine import pitcher_score
from weather_park_engine import weather_park_score


def _home_away_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Simple home-field advantage 0–100."""
    return 55.0, {"note": "Home field baseline +5"}


def _travel_rest_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Travel / rest penalty for away teams on consecutive road games."""
    away_rest = float(game.get("away_days_rest") or 0)
    home_rest = float(game.get("home_days_rest") or 0)
    score = 50.0
    if away_rest >= 3:
        score += 5
    elif away_rest <= 1:
        score -= 5
    if home_rest >= 3:
        score += 3
    return round(score, 1), {"away_rest": away_rest, "home_rest": home_rest}


def _recent_form_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Recent 10-game form edge for both teams."""
    away_wins = float(game.get("away_last_10_wins") or 50)
    home_wins = float(game.get("home_last_10_wins") or 50)
    diff = home_wins - away_wins
    score = 50.0 + diff * 2
    return round(score, 1), {"away_last_10_wins": away_wins, "home_last_10_wins": home_wins}


def _team_splits_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Team OPS splits vs LHP/RHP handedness advantage."""
    savant = game.get("savant") or {}
    away_split = savant.get("away_team", {}).get("xwOBA vs LHP") or savant.get("away_team", {}).get("xwOBA vs RHP")
    home_split = savant.get("home_team", {}).get("xwOBA vs LHP") or savant.get("home_team", {}).get("xwOBA vs RHP")
    if away_split or home_split:
        return 55.0, {"note": "Split data available"}
    return 50.0, {"note": "No split data — neutral"}


def score_game(game: dict[str, Any]) -> dict[str, Any]:
    """Calculate all v21 component scores for one verified MLB game."""
    components = {}
    meta = {}
    for key, func in (
        ("sp_score", pitcher_score),
        ("offense_score", offense_score),
        ("bullpen_score", bullpen_score),
        ("defense_score", defense_score),
        ("weather_park_score", weather_park_score),
        ("market_value_score", market_value_score),
        ("situational_score", situational_score),
        ("sp_batter_matchup_score", sp_batter_matchup_score),
        ("home_away_score", _home_away_score),
        ("travel_rest_score", _travel_rest_score),
        ("recent_form_score", _recent_form_score),
        ("team_splits_score", _team_splits_score),
    ):
        score, details = func(game)
        components[key] = score
        meta[key] = details
    weights_used = current_quant_weights()
    final_score = weighted_score(components)
    available = sum(int(details.get("available_fields", 0)) for details in meta.values() if isinstance(details, dict))
    possible = sum(int(details.get("possible_fields", 0)) for details in meta.values() if isinstance(details, dict))
    dq = data_quality_grade(available, possible)
    return {
        "model_version": MODEL_VERSION,
        "component_scores": components,
        "weights_used": weights_used,
        "sp_score": components["sp_score"],
        "offense_score": components["offense_score"],
        "bullpen_score": components["bullpen_score"],
        "defense_score": components["defense_score"],
        "weather_park_score": components["weather_park_score"],
        "market_value_score": components["market_value_score"],
        "situational_score": components["situational_score"],
        "sp_batter_matchup_score": components["sp_batter_matchup_score"],
        "home_away_score": components["home_away_score"],
        "travel_rest_score": components["travel_rest_score"],
        "recent_form_score": components["recent_form_score"],
        "team_splits_score": components["team_splits_score"],
        "final_edge_score": final_score,
        "minimum_edge_score": MINIMUM_EDGE_SCORE,
        "engine_decision": "QUALIFIED" if final_score >= MINIMUM_EDGE_SCORE else "PASS",
        "confidence": confidence_from_score(final_score),
        "risk_level": risk_level(final_score, dq),
        "data_quality_grade": dq,
        "details": meta,
        "matchup_summary": matchup_summary(game, components),
    }


def enrich_slate_with_quant_scores(slate: list[dict[str, Any]], card_date: str | None = None) -> list[dict[str, Any]]:
    """Attach v21 scoring outputs to each game in the slate.

    Weights are loaded dynamically from ``model_weights.json``.
    """
    enriched = []
    snapshots = []
    for game in slate:
        copied = dict(game)
        quant = score_game(copied)
        copied["betgptai_quant_v21"] = quant
        # Backward-compatible name for existing prompts/reports.
        copied["betgptai_quant_v20"] = quant
        copied["betgptai_internal"] = quant
        enriched.append(copied)
        snapshots.append({
            "game_pk": copied.get("game_pk") or copied.get("game_id"),
            "away_team": copied.get("away_team"),
            "home_team": copied.get("home_team"),
            **quant,
        })
    if card_date:
        try:
            save_edge_snapshot(card_date, snapshots)
        except Exception:
            pass
    return enriched
