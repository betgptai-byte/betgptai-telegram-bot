"""Internal BETGPTAI model engines.

These helpers make the bot smarter without making Telegram cards noisy.  They
calculate value, pitch-matchup, NRFI, F5, team-total, strikeout, and HR-watch
signals from the structured slate.  The returned fields are for prompts,
owner-only reports, and confidence scoring only; member cards should never show
raw formulas or provider names.
"""

from __future__ import annotations

import math
import re
from typing import Any


UNAVAILABLE = "unavailable"


def american_implied_probability(american_odds: int | float | None) -> float | None:
    """Convert American odds into implied probability as a decimal.

    Examples:
    - -150 -> 0.600
    - +120 -> 0.455
    """
    if not isinstance(american_odds, (int, float)) or isinstance(american_odds, bool):
        return None
    if american_odds < 0:
        return abs(float(american_odds)) / (abs(float(american_odds)) + 100)
    return 100 / (float(american_odds) + 100)


def _number(value: Any) -> float | None:
    """Read a numeric metric even when an API sends it as a formatted string."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        cleaned = value.replace("%", "").replace("+", "").strip()
        try:
            number = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _percent(value: Any) -> float | None:
    """Normalize percent-like metrics to their displayed percent scale."""
    number = _number(value)
    if number is None:
        return None
    return number * 100 if 0 < number <= 1 else number


def _has_data(value: Any) -> bool:
    """Return True when an optional source contains usable information."""
    if value in (None, "", UNAVAILABLE, [], {}):
        return False
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_data(item) for item in value)
    return True


def _side_for_team(game: dict[str, Any], team_name: Any) -> str | None:
    """Map a team-like selection back to away/home when possible."""
    normalized = re.sub(r"[^a-z0-9]", "", str(team_name or "").lower())
    if not normalized:
        return None
    for side in ("away", "home"):
        team = re.sub(r"[^a-z0-9]", "", str(game.get(f"{side}_team", "")).lower())
        nick = re.sub(
            r"[^a-z0-9]", "",
            str(game.get(f"{side}_team", "")).split()[-1].lower()
            if str(game.get(f"{side}_team", "")).split()
            else "",
        )
        if team and (team in normalized or normalized in team):
            return side
        if len(nick) >= 3 and nick in normalized:
            return side
    return None


def _metric(game: dict[str, Any], side: str, section: str, key: str) -> float | None:
    """Read one numeric Savant metric from away/home sections."""
    savant = game.get("savant")
    if not isinstance(savant, dict):
        return None
    payload = savant.get(f"{side}_{section}")
    if isinstance(payload, dict):
        return _number(payload.get(key))
    return None


def _opponent(side: str) -> str:
    return "home" if side == "away" else "away"


def _pitcher_edge(game: dict[str, Any], side: str) -> float:
    """Estimate starter edge from predictive pitcher metrics.

    Lower xERA, lower barrel/hard-hit allowed, and higher whiff/chase improve a
    side.  This is intentionally a bounded heuristic, not a claim of certainty.
    """
    other = _opponent(side)
    score = 0.0

    side_xera = _metric(game, side, "pitcher", "xERA")
    other_xera = _metric(game, other, "pitcher", "xERA")
    if side_xera is not None and other_xera is not None:
        score += max(-18, min(18, (other_xera - side_xera) * 8))

    side_whiff = _percent(_metric(game, side, "pitcher", "Whiff %"))
    other_whiff = _percent(_metric(game, other, "pitcher", "Whiff %"))
    if side_whiff is not None and other_whiff is not None:
        score += max(-10, min(10, (side_whiff - other_whiff) * 0.6))

    side_barrel = _percent(_metric(game, side, "pitcher", "Barrel %"))
    other_barrel = _percent(_metric(game, other, "pitcher", "Barrel %"))
    if side_barrel is not None and other_barrel is not None:
        score += max(-8, min(8, (other_barrel - side_barrel) * 0.7))

    return score


def _offense_edge(game: dict[str, Any], side: str) -> float:
    """Estimate lineup/team edge from xwOBA and contact quality."""
    other = _opponent(side)
    score = 0.0
    side_xwoba = _metric(game, side, "team", "xwOBA")
    other_xwoba = _metric(game, other, "team", "xwOBA")
    if side_xwoba is not None and other_xwoba is not None:
        score += max(-15, min(15, (side_xwoba - other_xwoba) * 120))

    side_barrel = _percent(_metric(game, side, "team", "Barrel %"))
    other_barrel = _percent(_metric(game, other, "team", "Barrel %"))
    if side_barrel is not None and other_barrel is not None:
        score += max(-8, min(8, (side_barrel - other_barrel) * 0.7))
    return score


def _recent_form_edge(game: dict[str, Any], side: str) -> float:
    """Small supporting bump from last-five form; never a primary driver."""
    form = game.get(f"{side}_recent_form")
    other = game.get(f"{_opponent(side)}_recent_form")
    if not isinstance(form, dict) or not isinstance(other, dict):
        return 0.0
    wins = _number(form.get("wins")) or 0
    other_wins = _number(other.get("wins")) or 0
    runs = (_number(form.get("runs_scored")) or 0) - (_number(form.get("runs_allowed")) or 0)
    other_runs = (_number(other.get("runs_scored")) or 0) - (_number(other.get("runs_allowed")) or 0)
    return max(-8, min(8, (wins - other_wins) * 1.5 + (runs - other_runs) * 0.12))


def _park_weather_total_edge(game: dict[str, Any]) -> float:
    """Estimate whether park/weather nudges scoring up or down."""
    park = str(game.get("park_factor", "")).lower()
    score = 0.0
    if "extreme hitter" in park:
        score += 12
    elif "hitter" in park or "hr-friendly" in park:
        score += 7
    elif "pitcher" in park:
        score -= 7

    weather = game.get("weather")
    if isinstance(weather, dict):
        temp = _number(weather.get("temperature"))
        wind = _number(weather.get("wind_speed"))
        precip = _number(weather.get("precipitation_probability"))
        if temp is not None:
            score += 4 if temp >= 80 else -3 if temp <= 55 else 0
        if wind is not None:
            score += 3 if wind >= 12 else 0
        if precip is not None and precip >= 40:
            score -= 3
    return score


def _pitch_type_score(game: dict[str, Any], side: str) -> float:
    """Score arsenal fit using available pitch mix fields only."""
    savant = game.get("savant")
    if not isinstance(savant, dict):
        return 0.0
    key = "away_pitcher_vs_home" if side == "away" else "home_pitcher_vs_away"
    matchup = savant.get("pitch_type_matchups", {}).get(key)
    if not isinstance(matchup, dict):
        return 0.0
    score = 0.0
    for pitch in matchup.get("arsenal", [])[:4]:
        if not isinstance(pitch, dict):
            continue
        whiff = _percent(pitch.get("whiff_rate"))
        xwoba = _number(pitch.get("xwOBA_allowed"))
        velo = _number(pitch.get("velocity"))
        if whiff is not None and whiff >= 30:
            score += 2
        if xwoba is not None and xwoba <= 0.300:
            score += 2
        if velo is not None and velo >= 95:
            score += 1
    return min(score, 8)


def projected_probability_for_side(game: dict[str, Any], side: str) -> float:
    """Create a bounded projected win probability for one team side."""
    score = 50.0
    score += 3 if side == "home" else -1
    score += _pitcher_edge(game, side)
    score += _offense_edge(game, side)
    score += _recent_form_edge(game, side)
    score += _pitch_type_score(game, side) * 0.4
    return max(0.36, min(0.72, score / 100))


def value_for_price(game: dict[str, Any], price: dict[str, Any]) -> dict[str, Any]:
    """Calculate implied probability, model projection, and edge for one price."""
    odds = price.get("price")
    implied = american_implied_probability(odds)
    projected: float | None = None

    market = price.get("market")
    if market == "h2h":
        side = _side_for_team(game, price.get("outcome"))
        if side:
            projected = projected_probability_for_side(game, side)
    elif market == "spreads":
        side = _side_for_team(game, price.get("outcome"))
        point = _number(price.get("point"))
        if side and point is not None:
            projected = projected_probability_for_side(game, side) + (0.04 if point > 0 else -0.03)
    elif market == "totals":
        point = _number(price.get("point"))
        total_edge = _park_weather_total_edge(game)
        if point is not None:
            baseline = 0.50 + max(-0.08, min(0.08, total_edge / 100))
            projected = baseline if str(price.get("outcome", "")).lower() == "over" else 1 - baseline

    edge = projected - implied if projected is not None and implied is not None else None
    return {
        "implied_probability": round(implied, 4) if implied is not None else None,
        "projected_probability": round(projected, 4) if projected is not None else None,
        "edge_percentage": round(edge * 100, 2) if edge is not None else None,
        "verified_positive_ev": bool(edge is not None and edge > 0),
    }


def _candidate_label(game: dict[str, Any], price: dict[str, Any]) -> str:
    point = price.get("point")
    if price.get("market") == "spreads" and isinstance(point, (int, float)):
        return f"{price.get('outcome')} {point:+g}"
    if price.get("market") == "totals" and isinstance(point, (int, float)):
        return f"{price.get('outcome')} {point:g} ({game.get('away_team')} @ {game.get('home_team')})"
    return str(price.get("outcome") or "Unknown")


def _value_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank all real market prices by internal edge."""
    candidates: list[dict[str, Any]] = []
    for game in games:
        for price in game.get("best_available_prices", []):
            value = value_for_price(game, price)
            if value["projected_probability"] is None:
                continue
            candidates.append({
                "game_id": game.get("game_id"),
                "market": price.get("market"),
                "selection": _candidate_label(game, price),
                "line": price.get("price"),
                **value,
            })
    candidates.sort(key=lambda item: item.get("edge_percentage") or -999, reverse=True)
    return candidates


def _nrfi_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find low-scoring first-inning profiles for internal NRFI ranking."""
    candidates = []
    for game in games:
        away_xera = _metric(game, "away", "pitcher", "xERA")
        home_xera = _metric(game, "home", "pitcher", "xERA")
        away_whip = _number((game.get("away_pitcher_stats") or {}).get("WHIP")) if isinstance(game.get("away_pitcher_stats"), dict) else None
        home_whip = _number((game.get("home_pitcher_stats") or {}).get("WHIP")) if isinstance(game.get("home_pitcher_stats"), dict) else None
        if away_xera is None and home_xera is None and away_whip is None and home_whip is None:
            continue
        score = 50
        for value in (away_xera, home_xera):
            score += 12 if value is not None and value <= 3.60 else -6 if value is not None and value >= 4.80 else 0
        for value in (away_whip, home_whip):
            score += 8 if value is not None and value <= 1.20 else -5 if value is not None and value >= 1.40 else 0
        score -= max(-10, min(10, _park_weather_total_edge(game) * 0.5))
        candidates.append({
            "game_id": game.get("game_id"),
            "selection": f"NRFI — {game.get('away_team')} @ {game.get('home_team')}",
            "score": round(max(1, min(100, score))),
        })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _f5_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank F5 moneyline-only candidates from starter and early matchup edges."""
    candidates = []
    for game in games:
        for side in ("away", "home"):
            score = 50 + _pitcher_edge(game, side) + _offense_edge(game, side) * 0.4
            score += _pitch_type_score(game, side)
            candidates.append({
                "game_id": game.get("game_id"),
                "selection": f"{game.get(f'{side}_team')} F5 ML",
                "score": round(max(1, min(100, score))),
            })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates


def _team_total_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank real team-total prices only when the odds feed supplies them."""
    candidates = []
    for game in games:
        for price in game.get("best_available_prices", []):
            if price.get("market") not in {"team_totals", "alternate_team_totals"}:
                continue
            value = value_for_price(game, price)
            candidates.append({
                "game_id": game.get("game_id"),
                "selection": _candidate_label(game, price),
                "line": price.get("price"),
                **value,
            })
    candidates.sort(key=lambda item: item.get("edge_percentage") or -999, reverse=True)
    return candidates


def _strikeout_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank pitcher K prop profiles; lines may be unavailable in the odds feed."""
    candidates = []
    for game in games:
        for side in ("away", "home"):
            whiff = _percent(_metric(game, side, "pitcher", "Whiff %"))
            chase = _percent(_metric(game, side, "pitcher", "Chase %"))
            score = 45
            if whiff is not None:
                score += max(-8, min(22, (whiff - 24) * 1.2))
            if chase is not None:
                score += max(-5, min(12, (chase - 28) * 0.8))
            candidates.append({
                "game_id": game.get("game_id"),
                "selection": f"{game.get(f'{side}_pitcher')} Strikeout Lean",
                "score": round(max(1, min(100, score))),
            })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return [item for item in candidates if item["score"] >= 55]


def _hr_watch_candidates(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank HR-watch profiles from batter contact quality and pitcher risk."""
    candidates = []
    for game in games:
        for side in ("away", "home"):
            batters = game.get("savant", {}).get(f"{side}_batters") if isinstance(game.get("savant"), dict) else None
            pitcher_side = _opponent(side)
            pitcher_barrel = _percent(_metric(game, pitcher_side, "pitcher", "Barrel %")) or 0
            park_boost = max(0, _park_weather_total_edge(game))
            if not isinstance(batters, list):
                continue
            for batter in batters:
                if not isinstance(batter, dict):
                    continue
                barrel = _percent(batter.get("Barrel %")) or 0
                hard_hit = _percent(batter.get("Hard Hit %")) or 0
                score = 35 + barrel * 1.1 + hard_hit * 0.25 + pitcher_barrel * 0.8 + park_boost * 0.4
                player = batter.get("player")
                if player:
                    candidates.append({
                        "game_id": game.get("game_id"),
                        "selection": f"HR Watch: {player}",
                        "score": round(max(1, min(100, score))),
                    })
    candidates.sort(key=lambda item: item["score"], reverse=True)
    return [item for item in candidates if item["score"] >= 60]


def enrich_slate_with_internal_models(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach compact internal engine output to each game and one slate summary."""
    value_candidates = _value_candidates(games)
    nrfi_candidates = _nrfi_candidates(games)
    f5_candidates = _f5_candidates(games)
    team_total_candidates = _team_total_candidates(games)
    strikeout_candidates = _strikeout_candidates(games)
    hr_watch_candidates = _hr_watch_candidates(games)

    by_game: dict[Any, dict[str, list[dict[str, Any]]]] = {}
    for label, candidates in (
        ("value", value_candidates),
        ("nrfi", nrfi_candidates),
        ("f5_moneyline", f5_candidates),
        ("team_totals", team_total_candidates),
        ("strikeouts", strikeout_candidates),
        ("home_runs", hr_watch_candidates),
    ):
        for candidate in candidates:
            by_game.setdefault(candidate.get("game_id"), {}).setdefault(label, []).append(candidate)

    for game in games:
        game["betgptai_internal"] = {
            "model_note": "Internal scoring only; do not display raw details to members.",
            "value_edges": by_game.get(game.get("game_id"), {}).get("value", [])[:5],
            "nrfi_candidates": by_game.get(game.get("game_id"), {}).get("nrfi", [])[:2],
            "f5_candidates": by_game.get(game.get("game_id"), {}).get("f5_moneyline", [])[:2],
            "team_total_candidates": by_game.get(game.get("game_id"), {}).get("team_totals", [])[:2],
            "strikeout_candidates": by_game.get(game.get("game_id"), {}).get("strikeouts", [])[:2],
            "home_run_candidates": by_game.get(game.get("game_id"), {}).get("home_runs", [])[:2],
        }

    summary = {
        "value_engine_count": sum(1 for item in value_candidates if item.get("verified_positive_ev")),
        "nrfi_candidates": len(nrfi_candidates),
        "f5_candidates": len(f5_candidates),
        "team_total_candidates": len(team_total_candidates),
        "strikeout_candidates": len(strikeout_candidates),
        "home_run_candidates": len(hr_watch_candidates),
        "top_value_edges": value_candidates[:8],
    }
    if games:
        games[0].setdefault("betgptai_slate_summary", summary)
    return games


def slate_engine_summary(games: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the daily internal engine summary saved on the first game."""
    for game in games:
        summary = game.get("betgptai_slate_summary")
        if isinstance(summary, dict):
            return summary
    return {
        "value_engine_count": 0,
        "nrfi_candidates": 0,
        "f5_candidates": 0,
        "team_total_candidates": 0,
        "strikeout_candidates": 0,
        "home_run_candidates": 0,
        "top_value_edges": [],
    }


def value_context_for_pick(
    games: list[dict[str, Any]], pick: dict[str, Any] | None
) -> dict[str, Any]:
    """Find internal value context for a displayed pick when possible."""
    if not pick:
        return {"verified_positive_ev": False}
    selection = re.sub(r"[^a-z0-9.+-]", "", str(pick.get("selection", "")).lower())
    pick_game = str(pick.get("game_id")) if pick.get("game_id") is not None else None
    best: dict[str, Any] | None = None
    for candidate in slate_engine_summary(games).get("top_value_edges", []):
        if pick_game and str(candidate.get("game_id")) != pick_game:
            continue
        candidate_text = re.sub(r"[^a-z0-9.+-]", "", str(candidate.get("selection", "")).lower())
        if selection and candidate_text and (selection in candidate_text or candidate_text in selection):
            best = candidate
            break
    return best or {"verified_positive_ev": False}
