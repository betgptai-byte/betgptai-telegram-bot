"""BETGPTAI ELITE QUANT ENGINE v20.0.

The engine calculates verified component scores. AI may rank/explain these
outputs, but should never invent missing stats.
"""

from __future__ import annotations

from typing import Any

from bullpen_engine import bullpen_score
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


def score_game(game: dict[str, Any]) -> dict[str, Any]:
    """Calculate all v20 component scores for one verified MLB game."""
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
    """Attach v20 scoring outputs to each game in the slate."""
    enriched = []
    snapshots = []
    for game in slate:
        copied = dict(game)
        quant = score_game(copied)
        copied["betgptai_quant_v20"] = quant
        # Backward-compatible name for existing prompts/reports.
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
