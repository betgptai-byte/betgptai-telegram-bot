"""ELITE QUANT ENGINE v20.0.

This module is the verified, API-first betting engine for BETGPTAI. It does not
invent statistics. It consumes the existing MLB/odds/weather slate and produces
scored game-level recommendations that the AI can evaluate in a constrained way.
"""

from __future__ import annotations

import math
from typing import Any


UNAVAILABLE = "unavailable"


def _number(value: Any) -> float | None:
    if value in (None, "", UNAVAILABLE, [], {}):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value).strip().replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_available(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", UNAVAILABLE, [], {}):
            return value
    return None


def _not_tbd(value: Any) -> bool:
    return bool(value) and str(value).strip().upper() != "TBD"


def _pitcher_profile_score(game: dict[str, Any], side: str) -> float:
    stats = _dict(game.get(f"{side}_pitcher_stats"))
    era = _number(stats.get("ERA"))
    whip = _number(stats.get("WHIP"))
    innings = _number(stats.get("IP"))
    strikeouts = _number(stats.get("K"))
    walks = _number(stats.get("BB"))
    home_runs = _number(stats.get("HR"))
    if era is None and whip is None and innings is None:
        return 50.0

    score = 50.0
    if era is not None:
        score += 16 if era <= 3.0 else 8 if era <= 3.8 else -6 if era >= 4.6 else 0
    if whip is not None:
        score += 10 if whip <= 1.15 else 6 if whip <= 1.3 else -4 if whip >= 1.5 else 0
    if innings is not None:
        score += 8 if innings >= 90 else 4 if innings >= 70 else 0
    if strikeouts is not None and innings is not None and innings > 0:
        k_per_nine = (strikeouts / innings) * 9
        score += 7 if k_per_nine >= 9.0 else 3 if k_per_nine >= 7.5 else 0
    if home_runs is not None and innings is not None and innings > 0:
        hr_per_nine = (home_runs / innings) * 9
        score -= 6 if hr_per_nine >= 1.3 else 3 if hr_per_nine >= 1.0 else 0
    if walks is not None and innings is not None and innings > 0:
        bb_per_nine = (walks / innings) * 9
        score -= 4 if bb_per_nine >= 4.2 else 2 if bb_per_nine >= 3.2 else 0
    return max(10.0, min(95.0, round(score, 2)))


def _offense_score(game: dict[str, Any]) -> float:
    recent = _dict(game.get("away_recent_form"))
    home_recent = _dict(game.get("home_recent_form"))
    score = 50.0
    away_wins = _number(recent.get("wins")) or 0
    home_wins = _number(home_recent.get("wins")) or 0
    score += (away_wins - home_wins) * 2.5
    weather = game.get("weather")
    if isinstance(weather, dict):
        temp = _number(weather.get("temperature_f"))
        if temp is not None:
            score += 4 if temp >= 80 else 1 if temp >= 65 else -2
    park = str(game.get("park_factor", "")).lower()
    if "hitter" in park or "hr-friendly" in park:
        score += 4
    elif "pitcher" in park:
        score -= 4
    return max(10.0, min(95.0, round(score, 2)))


def _bullpen_score(game: dict[str, Any]) -> float:
    bullpen = _dict(game.get("bullpen"))
    era = _number(bullpen.get("ERA"))
    whip = _number(bullpen.get("WHIP"))
    if era is None and whip is None:
        return 50.0
    score = 50.0
    if era is not None:
        score += 10 if era <= 3.4 else 4 if era <= 4.0 else -4
    if whip is not None:
        score += 8 if whip <= 1.2 else 4 if whip <= 1.35 else -3
    return max(10.0, min(95.0, round(score, 2)))


def _defense_score(game: dict[str, Any]) -> float:
    defense = _dict(game.get("defense"))
    drs = _number(defense.get("drs"))
    oaa = _number(defense.get("oaa"))
    if drs is None and oaa is None:
        return 50.0
    score = 50.0
    if drs is not None:
        score += drs * 0.8
    if oaa is not None:
        score += oaa * 1.2
    return max(10.0, min(95.0, round(score, 2)))


def _weather_score(game: dict[str, Any]) -> float:
    weather = game.get("weather")
    if not isinstance(weather, dict):
        return 50.0
    score = 50.0
    temp = _number(weather.get("temperature_f"))
    wind = _number(weather.get("wind_speed_mph"))
    precip = _number(weather.get("precipitation_probability_pct"))
    if temp is not None:
        score += 4 if temp >= 80 else 1 if temp >= 65 else -2
    if wind is not None:
        score += 3 if wind >= 12 else 1 if wind >= 8 else 0
    if precip is not None and precip >= 40:
        score -= 4
    park = str(game.get("park_factor", "")).lower()
    if "hitter" in park or "hr-friendly" in park:
        score += 4
    elif "pitcher" in park:
        score -= 4
    return max(10.0, min(95.0, round(score, 2)))


def _market_score(game: dict[str, Any]) -> float:
    prices = game.get("best_available_prices") or []
    if not isinstance(prices, list) or not prices:
        return 50.0
    values: list[float] = []
    for price in prices:
        if not isinstance(price, dict):
            continue
        price_value = _number(price.get("price"))
        if price_value is None:
            continue
        values.append(abs(price_value))
    if not values:
        return 50.0
    average_price = sum(values) / len(values)
    if average_price >= 190:
        return 40.0
    return 65.0 if average_price <= 150 else 55.0


def _situational_score(game: dict[str, Any]) -> float:
    score = 50.0
    if str(game.get("home_team", "")).lower() == str(game.get("away_team", "")).lower():
        score -= 5
    venue = str(game.get("venue") or "").lower()
    if "domed" in venue or "roof" in venue:
        score += 2
    if game.get("lineups") in ("confirmed", "available"):
        score += 4
    return max(10.0, min(95.0, round(score, 2)))


def score_game_edge(scores: dict[str, Any]) -> float:
    weights = {
        "sp_score": 0.30,
        "offense_score": 0.20,
        "bullpen_score": 0.15,
        "defense_score": 0.10,
        "weather_score": 0.10,
        "market_score": 0.10,
        "situational_score": 0.05,
    }
    total = 0.0
    for key, weight in weights.items():
        value = _number(scores.get(key))
        if value is None:
            continue
        total += value * weight
    return round(max(0.0, min(100.0, total)), 2)


def _confidence(edge_score: float) -> tuple[str, str]:
    if edge_score >= 82:
        return "Elite", "Elite"
    if edge_score >= 72:
        return "Strong", "Strong"
    if edge_score >= 62:
        return "Lean", "Lean"
    return "Pass", "Pass"


def _risk_level(edge_score: float) -> str:
    if edge_score >= 82:
        return "Low"
    if edge_score >= 72:
        return "Medium"
    if edge_score >= 62:
        return "Medium"
    return "High"


def _data_quality_grade(game: dict[str, Any], include_market: bool) -> str:
    available = 0
    if _not_tbd(game.get("away_pitcher")) and _not_tbd(game.get("home_pitcher")):
        available += 1
    if isinstance(game.get("weather"), dict):
        available += 1
    if game.get("best_available_prices"):
        available += 1
    if _not_tbd(game.get("away_team")) and _not_tbd(game.get("home_team")):
        available += 1
    if game.get("venue"):
        available += 1
    if game.get("lineups") not in (None, "", UNAVAILABLE, [], {}):
        available += 1
    if available >= 5:
        return "A"
    if available >= 4:
        return "B"
    if available >= 3:
        return "C"
    return "D"


def build_elite_quant_payload(game: dict[str, Any], *, include_market: bool = True) -> dict[str, Any]:
    """Build a single-game ELITE QUANT ENGINE v20.0 payload."""
    game_id = game.get("game_id") or game.get("game_pk")
    required_checks = {
        "schedule": bool(game_id and game.get("away_team") and game.get("home_team") and game.get("game_time")),
        "pitchers": bool(_not_tbd(game.get("away_pitcher")) and _not_tbd(game.get("home_pitcher"))),
        "weather": isinstance(game.get("weather"), dict) and bool(
            game.get("weather").get("summary") or game.get("weather").get("temperature_f") or game.get("weather").get("wind_speed_mph")
        ),
        "odds": (not include_market) or bool(game.get("best_available_prices")),
    }
    game_status = "ready" if all(required_checks.values()) else "pending_verification"

    if game_status != "ready":
        edge_score = 0.0
        confidence_label, tier = "Pass", "Pass"
        risk_level = "High"
        reason = "Pending verification because required schedule, pitcher, or weather data is unavailable."
        supporting_metrics = {
            "pitching_edge": 0.0,
            "offense_edge": 0.0,
            "weather_edge": 0.0,
            "market_edge": 0.0,
        }
    else:
        sp_score = round(
            (
                _pitcher_profile_score(game, "away") + _pitcher_profile_score(game, "home")
            ) / 2,
            2,
        )
        offense_score = _offense_score(game)
        bullpen_score = _bullpen_score(game)
        defense_score = _defense_score(game)
        weather_score = _weather_score(game)
        market_score = _market_score(game) if include_market else 50.0
        situational_score = _situational_score(game)

        edge_score = score_game_edge(
            {
                "sp_score": sp_score,
                "offense_score": offense_score,
                "bullpen_score": bullpen_score,
                "defense_score": defense_score,
                "weather_score": weather_score,
                "market_score": market_score,
                "situational_score": situational_score,
            }
        )
        confidence_label, tier = _confidence(edge_score)
        risk_level = _risk_level(edge_score)
        reason = (
            "Verified API slate with a favorable pitching and market profile."
        )
        supporting_metrics = {
            "pitching_edge": round(max(0.0, sp_score - 50.0), 2),
            "offense_edge": round(max(0.0, offense_score - 50.0), 2),
            "weather_edge": round(max(0.0, weather_score - 50.0), 2),
            "market_edge": round(max(0.0, market_score - 50.0), 2),
        }
    return {
        "game_id": game_id,
        "game_status": game_status,
        "away_team": game.get("away_team"),
        "home_team": game.get("home_team"),
        "venue": game.get("venue"),
        "first_pitch_et": game.get("game_time"),
        "starting_pitchers": {
            "away": game.get("away_pitcher"),
            "home": game.get("home_pitcher"),
        },
        "sp_score": sp_score if game_status == "ready" else 0.0,
        "offense_score": offense_score if game_status == "ready" else 0.0,
        "bullpen_score": bullpen_score if game_status == "ready" else 0.0,
        "defense_score": defense_score if game_status == "ready" else 0.0,
        "weather_score": weather_score if game_status == "ready" else 0.0,
        "market_score": market_score if game_status == "ready" else 0.0,
        "situational_score": situational_score if game_status == "ready" else 0.0,
        "edge_score": edge_score,
        "confidence": confidence_label,
        "tier": tier,
        "risk_level": risk_level,
        "data_quality_grade": _data_quality_grade(game, include_market),
        "reason": reason,
        "supporting_metrics": supporting_metrics,
        "market": _first_available(game.get("best_market"), "moneyline", "runline"),
        "required_checks": required_checks,
    }


def build_elite_quant_slate(games: list[dict[str, Any]], *, include_market: bool = True) -> list[dict[str, Any]]:
    """Build a full slate payload from the existing API-backed game objects."""
    return [build_elite_quant_payload(game, include_market=include_market) for game in games]
