"""Verified enrichment layer for the owner-only MLB War Room.

The War Room should show what the APIs actually verified. This module converts
the combined MLB slate into explicit game/pitcher/weather/market contexts and
tracks missing fields so the report can grade data quality honestly.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from game_time import format_game_clock


UNAVAILABLE = "unavailable"
CORE_PITCHER_FIELDS = (
    "ERA", "WHIP", "IP", "H", "K", "BB", "HR",
    "K%", "BB%", "K-BB%", "HR/9", "FIP", "xFIP", "SIERA",
    "xERA", "xBA", "xSLG", "xwOBA", "HardHit%", "Barrel%",
    "Whiff%", "Chase%", "Pitch Mix", "Avg Velocity",
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _present(value: Any) -> bool:
    return value not in (None, "", UNAVAILABLE, "Unavailable", "N/A", [], {})


def _num(value: Any) -> float | None:
    if not _present(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", ""))
    except ValueError:
        return None


def _fmt(value: Any) -> Any:
    return value if _present(value) else "N/A"


def _ip_to_float(value: Any) -> float | None:
    """Convert MLB innings strings like 42.1 into baseball innings float."""
    if value in (None, "", UNAVAILABLE):
        return None
    text = str(value)
    try:
        whole, _, frac = text.partition(".")
        innings = float(int(whole))
        if frac == "1":
            innings += 1 / 3
        elif frac == "2":
            innings += 2 / 3
        elif frac:
            innings += float(f"0.{frac}")
        return innings
    except Exception:
        return _num(value)


def _estimated_k_pct(stats: dict[str, Any]) -> str:
    strikeouts = _num(stats.get("K"))
    innings = _ip_to_float(stats.get("IP"))
    if strikeouts is None or innings is None or innings <= 0:
        return "N/A"
    # Approximation when batters faced is unavailable: IP * 4.3 BF/IP.
    bf_estimate = innings * 4.3
    return f"{round((strikeouts / bf_estimate) * 100, 1)}% est"


def _estimated_bb_pct(stats: dict[str, Any]) -> str:
    walks = _num(stats.get("BB"))
    innings = _ip_to_float(stats.get("IP"))
    if walks is None or innings is None or innings <= 0:
        return "N/A"
    bf_estimate = innings * 4.3
    return f"{round((walks / bf_estimate) * 100, 1)}% est"


def _estimated_kbb_pct(stats: dict[str, Any]) -> str:
    strikeouts = _num(stats.get("K"))
    walks = _num(stats.get("BB"))
    innings = _ip_to_float(stats.get("IP"))
    if strikeouts is None or walks is None or innings is None or innings <= 0:
        return "N/A"
    bf_estimate = innings * 4.3
    return f"{round(((strikeouts - walks) / bf_estimate) * 100, 1)}% est"


def _hr_per_9(stats: dict[str, Any]) -> str:
    hr = _num(stats.get("HR"))
    innings = _ip_to_float(stats.get("IP"))
    if hr is None or innings is None or innings <= 0:
        return "N/A"
    return f"{round((hr / innings) * 9, 2)} calc"


def _first(*values: Any) -> Any:
    for value in values:
        if _present(value):
            return value
    return "N/A"


def _weather_summary(weather: Any) -> str:
    if not isinstance(weather, dict) or not weather:
        return "N/A"
    temp = _first(weather.get("temperature_f"), weather.get("temperature"))
    wind = _first(weather.get("wind_speed_mph"), weather.get("wind_speed"))
    direction = _first(weather.get("wind_direction"), weather.get("wind_direction_degrees"))
    humidity = _first(weather.get("humidity"))
    summary = weather.get("summary")
    if _present(summary):
        return str(summary)
    parts = []
    if temp != "N/A":
        parts.append(f"{temp}°F" if isinstance(temp, (int, float)) else str(temp))
    if wind != "N/A":
        suffix = f" {direction}" if direction != "N/A" else ""
        parts.append(f"wind {wind} mph{suffix}")
    if humidity != "N/A":
        parts.append(f"humidity {humidity}%")
    return ", ".join(parts) if parts else "N/A"


def _market_context(game: dict[str, Any]) -> dict[str, Any]:
    prices = _list(game.get("best_available_prices"))
    context = {"ML": "N/A", "RL": "N/A", "total": "N/A", "team_totals": "N/A", "odds_found": bool(prices)}
    moneylines, spreads, totals, team_totals = [], [], [], []
    for price in prices:
        market = price.get("market")
        outcome = price.get("description") or price.get("outcome")
        point = price.get("point")
        p = price.get("price")
        if market == "h2h":
            moneylines.append(f"{outcome} {p}")
        elif market == "spreads":
            spreads.append(f"{outcome} {point:+g} {p}" if isinstance(point, (int, float)) else f"{outcome} {p}")
        elif market == "totals":
            totals.append(f"{outcome} {point:g} {p}" if isinstance(point, (int, float)) else f"{outcome} {p}")
        elif market == "team_totals":
            team_totals.append(f"{outcome} {point:g} {p}" if isinstance(point, (int, float)) else f"{outcome} {p}")
    if moneylines:
        context["ML"] = " | ".join(moneylines[:2])
    if spreads:
        context["RL"] = " | ".join(spreads[:2])
    if totals:
        context["total"] = " | ".join(totals[:2])
    if team_totals:
        context["team_totals"] = " | ".join(team_totals[:4])
    return context


def _side_context(game: dict[str, Any], side: str) -> dict[str, Any]:
    savant = _dict(game.get("savant"))
    fangraphs = _dict(game.get("fangraphs"))
    stats = _dict(game.get(f"{side}_pitcher_stats"))
    savant_pitcher = _dict(savant.get(f"{side}_pitcher"))
    fg_pitcher = _dict(fangraphs.get(f"{side}_pitcher"))
    pitcher_name = _first(game.get(f"{side}_pitcher"), "TBD")
    return {
        "name": pitcher_name,
        "id": game.get(f"{side}_pitcher_id"),
        "hand": _fmt(game.get(f"{side}_pitcher_hand")),
        "ERA": _fmt(stats.get("ERA") or stats.get("era")),
        "WHIP": _fmt(stats.get("WHIP") or stats.get("whip")),
        "IP": _fmt(stats.get("IP") or stats.get("inningsPitched")),
        "H": _fmt(stats.get("H") or stats.get("hits")),
        "K": _fmt(stats.get("K") or stats.get("strikeOuts")),
        "BB": _fmt(stats.get("BB") or stats.get("baseOnBalls")),
        "HR": _fmt(stats.get("HR") or stats.get("homeRuns")),
        "K%": _first(fg_pitcher.get("K%"), savant_pitcher.get("K%"), _estimated_k_pct(stats)),
        "BB%": _first(fg_pitcher.get("BB%"), savant_pitcher.get("BB%"), _estimated_bb_pct(stats)),
        "K-BB%": _first(fg_pitcher.get("K-BB%"), savant_pitcher.get("K-BB%"), _estimated_kbb_pct(stats)),
        "HR/9": _first(fg_pitcher.get("HR/9"), stats.get("HR/9"), stats.get("homeRunsPer9"), _hr_per_9(stats)),
        "FIP": _fmt(fg_pitcher.get("FIP")),
        "xFIP": _fmt(fg_pitcher.get("xFIP")),
        "SIERA": _fmt(fg_pitcher.get("SIERA")),
        "xERA": _fmt(savant_pitcher.get("xERA")),
        "xBA": _fmt(savant_pitcher.get("xBA")),
        "xSLG": _fmt(savant_pitcher.get("xSLG")),
        "xwOBA": _fmt(savant_pitcher.get("xwOBA")),
        "HardHit%": _first(savant_pitcher.get("Hard Hit %"), savant_pitcher.get("HardHit%")),
        "Barrel%": _first(savant_pitcher.get("Barrel %"), savant_pitcher.get("Barrel%")),
        "Whiff%": _first(savant_pitcher.get("Whiff %"), savant_pitcher.get("Whiff%")),
        "Chase%": _first(savant_pitcher.get("Chase %"), savant_pitcher.get("Chase%")),
        "Pitch Mix": _first(savant_pitcher.get("pitch_mix"), savant_pitcher.get("Pitch Mix")),
        "Avg Velocity": _first(savant_pitcher.get("Fastball Velocity"), savant_pitcher.get("velocity")),
        "Last 5 Starts": "N/A",
        "Home/Away Splits": "N/A",
        "Day/Night Splits": "N/A",
        "statcast_found": bool(savant_pitcher),
        "fangraphs_found": bool(fg_pitcher),
        "pitcher_stats_found": bool(stats),
    }


def _missing_count(game_context: dict[str, Any], pitcher_context: dict[str, Any], weather: dict[str, Any], market: dict[str, Any]) -> int:
    missing = 0
    for value in (game_context.get("away_record"), game_context.get("home_record"), game_context.get("venue")):
        missing += 0 if _present(value) and value != "N/A" else 1
    for side in ("away", "home"):
        pitcher = pitcher_context.get(side, {})
        for field in CORE_PITCHER_FIELDS:
            missing += 0 if _present(pitcher.get(field)) and pitcher.get(field) != "N/A" else 1
    missing += 0 if weather.get("summary") != "N/A" else 1
    missing += 0 if market.get("odds_found") else 1
    return missing


def _pitcher_metric_coverage(pitcher_context: dict[str, Any]) -> float:
    total = len(CORE_PITCHER_FIELDS) * 2
    found = 0
    for side in ("away", "home"):
        pitcher = pitcher_context.get(side, {})
        found += sum(1 for field in CORE_PITCHER_FIELDS if _present(pitcher.get(field)) and pitcher.get(field) != "N/A")
    return found / total if total else 0.0


def _data_quality_grade(context: dict[str, Any]) -> str:
    starters_verified = context["debug"]["pitcher_stats_found"]
    odds = context["debug"]["odds_found"]
    weather = context["debug"]["weather_found"]
    coverage = context["debug"]["pitcher_metric_coverage"]
    if starters_verified and odds and weather and coverage >= 0.70:
        return "A"
    if starters_verified and coverage >= 0.50:
        return "B"
    if starters_verified or weather or odds:
        return "C"
    return "D"


def _overall_grade(context: dict[str, Any]) -> str:
    quant = _dict(context.get("quant_context"))
    dq = context.get("data_quality_grade")
    edge = _num(quant.get("final_edge_score")) or 0
    qualified = quant.get("engine_decision") == "QUALIFIED"
    if dq == "A" and qualified and edge >= 90:
        return "A+"
    if dq == "A" and qualified:
        return "A"
    if dq in {"A", "B"} and edge >= 82:
        return "A-"
    if dq == "B":
        return "B+"
    if dq == "C":
        return "B"
    return "C"


def _strongest_lean(game: dict[str, Any], context: dict[str, Any]) -> str:
    quant = _dict(context.get("quant_context"))
    if quant.get("engine_decision") != "QUALIFIED":
        return "PASS — no qualifying quant edge"
    prices = _list(game.get("best_available_prices"))
    h2h = [p for p in prices if p.get("market") == "h2h" and isinstance(p.get("price"), (int, float))]
    if not h2h:
        return "QUALIFIED game edge — market lean unavailable"
    h2h.sort(key=lambda p: abs(float(p.get("price"))))
    outcome = h2h[0].get("outcome") or h2h[0].get("description")
    return f"{outcome} ML — quant edge {quant.get('final_edge_score')}/100"


def enrich_war_room_game(game: dict[str, Any]) -> dict[str, Any]:
    """Return a fully enriched War Room context for one slate game."""
    verification = _dict(game.get("verification"))
    verification_score = int(verification.get("score") or 0)
    game_context = {
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "away_team": _fmt(game.get("away_team")),
        "home_team": _fmt(game.get("home_team")),
        "away_record": _fmt(game.get("away_record")),
        "home_record": _fmt(game.get("home_record")),
        "venue": _fmt(game.get("venue")),
        "game_time_et": format_game_clock(game.get("game_time"), status=game.get("status")),
        "status": _fmt(game.get("status")),
        "probable_starters": {
            "away": _fmt(game.get("away_pitcher")),
            "home": _fmt(game.get("home_pitcher")),
        },
    }
    pitcher_context = {
        "away": _side_context(game, "away"),
        "home": _side_context(game, "home"),
    }
    weather_context = {
        "summary": _weather_summary(game.get("weather")),
        "temp": _first(_dict(game.get("weather")).get("temperature_f"), _dict(game.get("weather")).get("temperature")),
        "wind": _first(_dict(game.get("weather")).get("wind_speed_mph"), _dict(game.get("weather")).get("wind_speed")),
        "wind_direction": _first(_dict(game.get("weather")).get("wind_direction"), _dict(game.get("weather")).get("wind_direction_degrees")),
        "humidity": _fmt(_dict(game.get("weather")).get("humidity")),
        "run_environment": _first(game.get("park_factor"), game.get("park_factor_label"), "neutral"),
    }
    market_context = _market_context(game)
    quant_context = _dict(game.get("betgptai_quant_v20") or game.get("betgptai_internal"))
    debug = {
        "game_pk": game_context["game_pk"],
        "records_found": game_context["away_record"] != "N/A" and game_context["home_record"] != "N/A",
        "weather_found": weather_context["summary"] != "N/A",
        "pitcher_stats_found": pitcher_context["away"]["pitcher_stats_found"] and pitcher_context["home"]["pitcher_stats_found"],
        "statcast_found": pitcher_context["away"]["statcast_found"] or pitcher_context["home"]["statcast_found"],
        "fangraphs_found": pitcher_context["away"]["fangraphs_found"] or pitcher_context["home"]["fangraphs_found"],
        "odds_found": bool(market_context.get("odds_found")),
        "pitcher_metric_coverage": round(_pitcher_metric_coverage(pitcher_context), 3),
        "verification_score": verification_score,
        "verification_alerts": verification.get("admin_alerts") or [],
    }
    context = {
        "game_context": game_context,
        "pitcher_context": pitcher_context,
        "weather_context": weather_context,
        "market_context": market_context,
        "quant_context": quant_context,
        "verification": verification,
        "verification_score": verification_score,
        "debug": debug,
    }
    context["missing_fields_count"] = _missing_count(game_context, pitcher_context, weather_context, market_context)
    context["data_quality_grade"] = _data_quality_grade(context)
    context["overall_grade"] = _overall_grade(context)
    context["strongest_lean"] = _strongest_lean(game, context)
    return context


def war_room_debug_rows(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one owner-debug row per enriched War Room game."""
    rows = []
    for game in games:
        context = game.get("war_room_enrichment") if isinstance(game.get("war_room_enrichment"), dict) else enrich_war_room_game(game)
        debug = dict(context.get("debug") or {})
        debug["missing_fields_count"] = context.get("missing_fields_count")
        debug["data_quality_grade"] = context.get("data_quality_grade")
        debug["verification_score"] = context.get("verification_score")
        debug["verification_alerts"] = context.get("debug", {}).get("verification_alerts") or []
        debug["game"] = f"{context['game_context'].get('away_team')} @ {context['game_context'].get('home_team')}"
        rows.append(debug)
    return rows
