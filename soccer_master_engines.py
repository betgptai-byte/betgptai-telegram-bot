"""Hidden Soccer Master System engines for BETGPTAI.

The public Telegram experience should stay simple: pick, time, line, confidence,
and one clean sentence.  This module quietly evaluates richer soccer context so
OpenAI/Claude and owner reports can make better decisions without exposing raw
statistics, source names, formulas, or model disagreements to members.
"""

from __future__ import annotations

import math
from typing import Any


UNAVAILABLE = "unavailable"


def _number(value: Any) -> float | None:
    """Safely parse numeric API fields, including percent strings."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.replace("%", "").strip())
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _has_data(value: Any) -> bool:
    if value in (None, "", UNAVAILABLE, [], {}):
        return False
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_data(item) for item in value)
    return True


def _rate(numerator: float, denominator: float) -> float | None:
    return round(100 * numerator / denominator, 1) if denominator else None


def _form_points(form: dict[str, Any] | str) -> float:
    if not isinstance(form, dict):
        return 0.0
    return (
        (_number(form.get("wins")) or 0) * 3
        + (_number(form.get("draws")) or 0)
        - (_number(form.get("losses")) or 0) * 1.5
    )


def _side_context(game: dict[str, Any], side: str, source: str) -> dict[str, Any] | None:
    """Read home/away internal context from source-specific payloads."""
    payload = game.get(source)
    if not isinstance(payload, dict):
        return None
    key = f"{side}_team"
    value = payload.get(key)
    return value if isinstance(value, dict) else None


def _xg_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Score attacking/defensive xG context when optional xG exists."""
    home = game.get("home_advanced")
    away = game.get("away_advanced")
    home_xg = _number((home or {}).get("xG")) if isinstance(home, dict) else None
    away_xg = _number((away or {}).get("xG")) if isinstance(away, dict) else None
    home_xga = _number((home or {}).get("xGA")) if isinstance(home, dict) else None
    away_xga = _number((away or {}).get("xGA")) if isinstance(away, dict) else None
    statsbomb_home = _side_context(game, "home", "statsbomb_context")
    statsbomb_away = _side_context(game, "away", "statsbomb_context")
    if home_xg is None and statsbomb_home:
        home_xg = _number(statsbomb_home.get("xG"))
    if away_xg is None and statsbomb_away:
        away_xg = _number(statsbomb_away.get("xG"))
    # Open-data match samples do not always have xGA directly. Use opponent xG
    # as the most honest available proxy, without displaying it publicly.
    if home_xga is None and statsbomb_away:
        home_xga = _number(statsbomb_away.get("xG"))
    if away_xga is None and statsbomb_home:
        away_xga = _number(statsbomb_home.get("xG"))
    if None in (home_xg, away_xg, home_xga, away_xga):
        return {"status": UNAVAILABLE, "score": 50}
    total_pressure = home_xg + away_xg + home_xga + away_xga
    score = 50 + max(-20, min(25, (total_pressure - 5.0) * 8))
    return {"status": "available", "score": round(score), "total_xg_pressure": round(total_pressure, 2)}


def _pace_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Use shots, shots on target, possession, and big chances if present."""
    total = 0.0
    seen = False
    for key in ("home_advanced", "away_advanced"):
        data = game.get(key)
        if not isinstance(data, dict):
            continue
        for metric, weight in (
            ("shots", 1.0), ("shots_on_target", 2.5),
            ("big_chances", 4.0), ("corners", 0.8),
        ):
            value = _number(data.get(metric))
            if value is not None:
                total += value * weight
                seen = True
    for side in ("home", "away"):
        data = _side_context(game, side, "statsbomb_context")
        if not isinstance(data, dict):
            continue
        for metric, weight in (
            ("shots", 1.0), ("shots_on_target", 2.5),
            ("pressures", 0.25), ("passes", 0.03),
            ("defensive_actions", 0.4), ("pace", 0.04),
        ):
            value = _number(data.get(metric))
            if value is not None:
                total += value * weight
                seen = True
    if not seen:
        return {"status": UNAVAILABLE, "score": 50}
    return {"status": "available", "score": round(max(1, min(100, 35 + total)))}


def _league_environment_engine(game: dict[str, Any]) -> dict[str, Any]:
    env = game.get("league_environment")
    if not isinstance(env, dict):
        return {"status": UNAVAILABLE, "score": 50}
    goals = _number(env.get("goals_per_match")) or 2.5
    btts = _number(env.get("btts_rate")) or 50
    over = _number(env.get("over_2_5_rate")) or 50
    score = 50 + (goals - 2.5) * 12 + (btts - 50) * 0.15 + (over - 50) * 0.15
    return {"status": "available", "score": round(max(1, min(100, score)))}


def _league_weight_adjustment(game: dict[str, Any]) -> int:
    """Apply hidden league confidence weighting requested by BETGPTAI.

    Positive leagues tend to produce clearer betting environments for this
    system. Defensive or volatile leagues are slightly reduced internally.
    """
    text = " ".join(
        str(game.get(key) or "")
        for key in ("competition", "competition_code", "area_name", "area_code", "stage")
    ).lower()
    boost_terms = (
        "mls", "major league soccer", "nwsl", "norwegian", "eliteserien",
        "swedish", "allsvenskan", "saudi pro", "world cup", "euro",
        "copa america", "gold cup", "international", "tournament",
    )
    reduce_terms = (
        "argentina", "uruguay", "brazil", "brasileiro", "serie b",
        "serie c", "promotion", "playout", "italian promotion",
    )
    boost = 6 if any(term in text for term in boost_terms) else 0
    reduction = 7 if any(term in text for term in reduce_terms) else 0
    return boost - reduction


def _home_away_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Blend home/away recent form, API team stats, and FBref trend context."""
    home_form = _form_points(game.get("home_recent"))
    away_form = _form_points(game.get("away_recent"))
    api_context = game.get("api_football_context")
    if isinstance(api_context, dict):
        home_stats = api_context.get("home_team_statistics")
        away_stats = api_context.get("away_team_statistics")
        for stats, direction in ((home_stats, 1), (away_stats, -1)):
            if not isinstance(stats, dict):
                continue
            fixtures = stats.get("fixtures") if isinstance(stats.get("fixtures"), dict) else {}
            wins = _number((fixtures.get("wins") or {}).get("total")) if isinstance(fixtures.get("wins"), dict) else None
            losses = _number((fixtures.get("loses") or {}).get("total")) if isinstance(fixtures.get("loses"), dict) else None
            edge = ((wins or 0) - (losses or 0)) * 0.4
            home_form += edge * direction
    score = 50 + max(-18, min(18, home_form - away_form))
    return {"status": "available", "score": round(max(1, min(100, score)))}


def _form_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Dedicated last-five form engine using Football-Data results."""
    home_form = _form_points(game.get("home_recent"))
    away_form = _form_points(game.get("away_recent"))
    if not home_form and not away_form:
        return {"status": UNAVAILABLE, "score": 50}
    score = 50 + max(-20, min(20, home_form - away_form))
    return {"status": "available", "score": round(max(1, min(100, score)))}


def _over_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Dedicated totals engine using xG, pace, and league scoring."""
    xg = _xg_engine(game)["score"]
    pace = _pace_engine(game)["score"]
    league = _league_environment_engine(game)["score"]
    timing = _goal_timing_engine(game)["score"]
    return {
        "status": "available" if max(xg, pace, league, timing) != 50 else UNAVAILABLE,
        "score": round((xg * 0.35) + (pace * 0.30) + (league * 0.25) + (timing * 0.10)),
    }


def _motivation_engine(game: dict[str, Any]) -> dict[str, Any]:
    context = game.get("motivation_context") if isinstance(game.get("motivation_context"), dict) else {}
    stage = str(context.get("stage") or game.get("stage") or "").lower()
    score = 50
    if any(word in stage for word in ("final", "semi", "quarter", "playoff", "knockout")):
        score += 15
    if game.get("world_cup_context") not in (None, "", UNAVAILABLE, {}, []):
        score += 10
    return {"status": "available", "score": min(score, 100)}


def _btts_engine(game: dict[str, Any]) -> dict[str, Any]:
    forms = [game.get("home_recent"), game.get("away_recent")]
    btts_counts = []
    matches = []
    for form in forms:
        if isinstance(form, dict):
            btts_counts.append(_number(form.get("btts_matches")) or 0)
            matches.append(_number(form.get("matches")) or 0)
    if not matches or not sum(matches):
        return {"status": UNAVAILABLE, "score": 50}
    btts_rate = _rate(sum(btts_counts), sum(matches)) or 50
    return {"status": "available", "score": round(max(1, min(100, 35 + btts_rate * 0.8))), "btts_rate": btts_rate}


def _corner_engine(game: dict[str, Any]) -> dict[str, Any]:
    profile = game.get("corners_profile")
    if isinstance(profile, dict):
        corners = _number(profile.get("combined_corners")) or _number(profile.get("average_corners"))
        if corners is not None:
            return {"status": "available", "score": round(max(1, min(100, 35 + corners * 4)))}
    return {"status": UNAVAILABLE, "score": 50}


def _goal_timing_engine(game: dict[str, Any]) -> dict[str, Any]:
    data = game.get("goal_timing")
    if isinstance(data, dict):
        late = _number(data.get("late_goal_rate")) or _number(data.get("late_goals"))
        if late is not None:
            return {"status": "available", "score": round(max(1, min(100, 45 + late * 0.8)))}
    late_goals = 0
    seen = False
    for side in ("home", "away"):
        context = _side_context(game, side, "statsbomb_context")
        if isinstance(context, dict):
            late_goals += int(_number(context.get("late_goals")) or 0)
            if context.get("goal_timing") not in (None, "", UNAVAILABLE, [], {}):
                seen = True
    if seen:
        return {"status": "available", "score": round(max(1, min(100, 48 + late_goals * 8)))}
    return {"status": UNAVAILABLE, "score": 50}


def _red_card_engine(game: dict[str, Any]) -> dict[str, Any]:
    referee = game.get("referee_tendencies")
    if isinstance(referee, dict):
        cards = _number(referee.get("red_cards_per_match")) or _number(referee.get("cards_per_match"))
        if cards is not None:
            return {"status": "available", "score": round(max(1, min(100, 45 + cards * 8)))}
    return {"status": UNAVAILABLE, "score": 50}


def _card_engine(game: dict[str, Any]) -> dict[str, Any]:
    """Evaluate yellow/red-card environment from API-Football fixture stats."""
    referee = game.get("referee_tendencies")
    if not isinstance(referee, dict):
        return {"status": UNAVAILABLE, "score": 50}
    values = [
        _number(referee.get(key)) or 0
        for key in ("home_yellow_cards", "away_yellow_cards")
    ]
    reds = [
        _number(referee.get(key)) or 0
        for key in ("home_red_cards", "away_red_cards")
    ]
    total = sum(values) + sum(reds) * 2
    return {"status": "available", "score": round(max(1, min(100, 40 + total * 8)))}


def _world_cup_engine(game: dict[str, Any]) -> dict[str, Any]:
    context = game.get("world_cup_context")
    if isinstance(context, dict) and _has_data(context):
        importance = _number(context.get("importance_score")) or 65
        return {"status": "available", "score": round(max(1, min(100, importance)))}
    competition = str(game.get("competition") or "").lower()
    if "world cup" in competition or "qualification" in competition or "qualifier" in competition:
        return {"status": "available", "score": 70}
    return {"status": UNAVAILABLE, "score": 50}


def _referee_engine(game: dict[str, Any]) -> dict[str, Any]:
    referee = game.get("referee_tendencies")
    if isinstance(referee, dict) and _has_data(referee):
        cards = _number(referee.get("cards_per_match")) or 0
        fouls = _number(referee.get("fouls_per_match")) or 0
        return {"status": "available", "score": round(max(1, min(100, 45 + cards * 4 + fouls * 0.3)))}
    return {"status": UNAVAILABLE, "score": 50}


def _elo_engine(game: dict[str, Any]) -> dict[str, Any]:
    home = game.get("home_elo")
    away = game.get("away_elo")
    home_elo = _number((home or {}).get("elo")) if isinstance(home, dict) else None
    away_elo = _number((away or {}).get("elo")) if isinstance(away, dict) else None
    if home_elo is None or away_elo is None:
        return {"status": UNAVAILABLE, "score": 50}
    edge = home_elo - away_elo + 55  # small home-field prior
    return {"status": "available", "score": round(max(1, min(100, 50 + edge / 18))), "elo_gap": round(edge)}


def _double_chance_dnb_engine(
    game: dict[str, Any], engines: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Hidden Double Chance / Draw No Bet stability engine.

    It favors team-strength edge, home/away context, and motivation, but never
    rejects a game when one of those feeds is missing.
    """
    elo = engines["elo"]["score"]
    home_away = engines["home_away"]["score"]
    motivation = engines["motivation"]["score"]
    home_form = _form_points(game.get("home_recent"))
    away_form = _form_points(game.get("away_recent"))
    form_edge = max(-12, min(12, home_form - away_form))
    score = round(
        (elo * 0.35)
        + (home_away * 0.30)
        + (motivation * 0.20)
        + ((50 + form_edge) * 0.15)
    )
    status = "available" if any(
        engines[name].get("status") == "available"
        for name in ("elo", "home_away", "motivation")
    ) or home_form or away_form else UNAVAILABLE
    return {"status": status, "score": max(1, min(100, score))}


def _candidate_scores(game: dict[str, Any], engines: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Blend engine scores into market-specific hidden candidate grades."""
    xg = engines["xg"]["score"]
    pace = engines["pace"]["score"]
    league = engines["league_environment"]["score"]
    btts = engines["btts"]["score"]
    corners = engines["corners"]["score"]
    timing = engines["goal_timing"]["score"]
    elo = engines["elo"]["score"]
    motivation = engines["motivation"]["score"]
    home_away = engines["home_away"]["score"]
    form = engines["form"]["score"]
    over = engines["over"]["score"]
    double_chance_dnb = engines["double_chance_dnb"]["score"]
    home_form = _form_points(game.get("home_recent"))
    away_form = _form_points(game.get("away_recent"))
    fbref = game.get("fbref_context")
    if isinstance(fbref, dict):
        home_fbref, away_fbref = fbref.get("home_team"), fbref.get("away_team")
        home_gd = _number((home_fbref or {}).get("goal_difference")) if isinstance(home_fbref, dict) else None
        away_gd = _number((away_fbref or {}).get("goal_difference")) if isinstance(away_fbref, dict) else None
        if home_gd is not None:
            home_form += max(-8, min(8, home_gd * 0.15))
        if away_gd is not None:
            away_form += max(-8, min(8, away_gd * 0.15))
    form_edge = max(-12, min(12, home_form - away_form))
    league_adjustment = _league_weight_adjustment(game)

    def adjusted(value: float) -> int:
        return max(1, min(100, round(value + league_adjustment)))

    return {
        "btts": adjusted((btts * 0.45) + (xg * 0.25) + (league * 0.20) + (pace * 0.10)),
        "over_2_5": adjusted((over * 0.60) + (xg * 0.15) + (pace * 0.15) + (timing * 0.10)),
        "double_chance": adjusted((double_chance_dnb * 0.70) + (form * 0.15) + (home_away * 0.15)),
        "moneyline": adjusted((elo * 0.30) + (home_away * 0.25) + (form * 0.25) + (motivation * 0.20)),
        "draw_no_bet": adjusted((double_chance_dnb * 0.65) + (home_away * 0.20) + (form * 0.15)),
        "corners": adjusted((corners * 0.65) + (pace * 0.35)),
    }


def enrich_soccer_master_system(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach hidden engine output and a slate summary to soccer fixtures."""
    source_counts = {
        "football_data_games": 0,
        "thesportsdb_games": 0,
        "clubelo_games": 0,
        "understat_games": 0,
        "serpapi_games": 0,
        "statsbomb_games": 0,
        "fbref_games": 0,
        "weather_games": 0,
        "odds_games": 0,
        "api_football_optional_games": 0,
    }
    engine_counts = {
        "xg_candidates": 0,
        "pace_candidates": 0,
        "league_environment_candidates": 0,
        "motivation_candidates": 0,
        "form_candidates": 0,
        "btts_candidates": 0,
        "over_candidates": 0,
        "home_away_candidates": 0,
        "corner_candidates": 0,
        "card_candidates": 0,
        "goal_timing_candidates": 0,
        "red_card_candidates": 0,
        "world_cup_candidates": 0,
        "referee_candidates": 0,
        "double_chance_dnb_candidates": 0,
    }
    all_candidates: list[dict[str, Any]] = []
    for game in slate:
        engines = {
            "xg": _xg_engine(game),
            "pace": _pace_engine(game),
            "league_environment": _league_environment_engine(game),
            "home_away": _home_away_engine(game),
            "form": _form_engine(game),
            "motivation": _motivation_engine(game),
            "btts": _btts_engine(game),
            "over": _over_engine(game),
            "corners": _corner_engine(game),
            "cards": _card_engine(game),
            "goal_timing": _goal_timing_engine(game),
            "red_card": _red_card_engine(game),
            "world_cup": _world_cup_engine(game),
            "referee": _referee_engine(game),
            "elo": _elo_engine(game),
        }
        engines["double_chance_dnb"] = _double_chance_dnb_engine(game, engines)
        for source_key, fields in (
            ("football_data_games", ("football_data_context",)),
            ("thesportsdb_games", ("sportsdb_context", "supplemental_soccer_data")),
            ("clubelo_games", ("home_elo", "away_elo")),
            ("understat_games", ("understat_context",)),
            ("serpapi_games", ("serpapi_context",)),
            ("statsbomb_games", ("statsbomb_context",)),
            ("fbref_games", ("fbref_context",)),
            ("weather_games", ("weather",)),
            ("odds_games", ("best_available_prices",)),
            ("api_football_optional_games", ("api_football_context",)),
        ):
            if any(_has_data(game.get(field)) for field in fields):
                source_counts[source_key] += 1
        for engine_name, key in (
            ("xg", "xg_candidates"),
            ("pace", "pace_candidates"),
            ("league_environment", "league_environment_candidates"),
            ("home_away", "home_away_candidates"),
            ("form", "form_candidates"),
            ("motivation", "motivation_candidates"),
            ("btts", "btts_candidates"),
            ("over", "over_candidates"),
            ("corners", "corner_candidates"),
            ("cards", "card_candidates"),
            ("goal_timing", "goal_timing_candidates"),
            ("red_card", "red_card_candidates"),
            ("world_cup", "world_cup_candidates"),
            ("referee", "referee_candidates"),
            ("double_chance_dnb", "double_chance_dnb_candidates"),
        ):
            if engines[engine_name].get("status") == "available":
                engine_counts[key] += 1
        candidates = _candidate_scores(game, engines)
        for market, score in candidates.items():
            all_candidates.append({
                "match_id": game.get("match_id"),
                "market": market,
                "score": max(1, min(100, score)),
            })
        game["soccer_internal"] = {
            "model_note": "Internal Soccer Master System only; do not display raw details to members.",
            "engines": engines,
            "market_scores": candidates,
        }
    all_candidates.sort(key=lambda item: item["score"], reverse=True)
    if slate:
        slate[0]["soccer_slate_summary"] = {
            **source_counts,
            **engine_counts,
            "top_internal_soccer_edges": all_candidates[:10],
        }
    return slate


def soccer_slate_summary(slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Read the hidden daily soccer summary from the slate."""
    for game in slate:
        summary = game.get("soccer_slate_summary")
        if isinstance(summary, dict):
            return summary
    return {}
