from __future__ import annotations

from elite_quant_engine import build_elite_quant_payload, score_game_edge


def test_score_game_edge_handles_unverified_data() -> None:
    payload = build_elite_quant_payload(
        {
            "game_id": 1,
            "away_team": "Yankees",
            "home_team": "Mets",
            "venue": "Citi Field",
            "game_time": "2026-07-04T18:10:00Z",
            "away_pitcher": "Max Fried",
            "home_pitcher": "Kodai Senga",
            "weather": {"temperature_f": 78, "wind_speed_mph": 12, "summary": "sunny"},
            "best_available_prices": [
                {"market": "h2h", "outcome": "Yankees", "price": -140},
                {"market": "h2h", "outcome": "Mets", "price": 120},
            ],
            "away_pitcher_stats": {"ERA": 3.2, "WHIP": 1.12, "IP": 98.0, "K": 103, "BB": 24, "HR": 10},
            "home_pitcher_stats": {"ERA": 4.1, "WHIP": 1.28, "IP": 85.0, "K": 88, "BB": 31, "HR": 14},
            "away_recent_form": {"wins": 4, "losses": 1},
            "home_recent_form": {"wins": 2, "losses": 3},
            "savant": {"pitcher_expected": {"xERA": 3.5}},
            "fangraphs": {"pitcher": {"FIP": 3.4}},
            "park_factor": "hitter-friendly",
        },
        include_market=True,
    )

    assert payload["game_status"] == "ready"
    assert payload["edge_score"] >= 0
    assert payload["data_quality_grade"] in {"A", "B", "C", "D"}


def test_score_game_edge_marks_pending_when_required_data_missing() -> None:
    payload = build_elite_quant_payload(
        {
            "game_id": 2,
            "away_team": "Dodgers",
            "home_team": "Padres",
            "venue": "Petco Park",
            "game_time": "2026-07-04T20:10:00Z",
            "away_pitcher": "TBD",
            "home_pitcher": "TBD",
            "weather": "unavailable",
        },
        include_market=False,
    )

    assert payload["game_status"] == "pending_verification"
    assert payload["edge_score"] == 0
    assert payload["confidence"] == "Pass"


def test_score_game_edge_is_greater_for_stronger_matchup() -> None:
    strong = score_game_edge({"sp_score": 88, "offense_score": 84, "bullpen_score": 77, "defense_score": 75, "weather_score": 70, "market_score": 82, "situational_score": 80})
    weak = score_game_edge({"sp_score": 55, "offense_score": 50, "bullpen_score": 48, "defense_score": 52, "weather_score": 40, "market_score": 45, "situational_score": 50})

    assert strong > weak
