"""Structured game-by-game MLB edge reports for BETGPTAI.

This engine consumes verified/enriched slate objects and never fabricates a
missing statistic, line, or price. Optional feeds become neutral scores and
explicit red flags rather than exceptions.
"""
from __future__ import annotations

import math
import os
from datetime import datetime
from typing import Any

SOURCE = "mlb_game_edge_engine_v1"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    if value in (None, "", "unavailable", [], {}) or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace("%", ""))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 1)


def _metric(value: Any, baseline: float, scale: float, *, lower: bool = False) -> float:
    number = _num(value)
    if number is None:
        return 0.0
    delta = (baseline - number) if lower else (number - baseline)
    return max(-18.0, min(18.0, delta * scale))


def _context_for(context: dict | None, game: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(context, dict):
        return {}
    game_id = str(game.get("game_pk") or game.get("game_id") or "")
    value = context.get(game_id) or context.get(game.get("game_pk")) or context.get(game.get("game_id"))
    return value if isinstance(value, dict) else context if any(k in context for k in ("away", "home", "weather", "lineups")) else {}


def _pitcher_facts(game: dict[str, Any], side: str, override: dict[str, Any]) -> dict[str, Any]:
    stats = _dict(override.get(side)) or _dict(game.get(f"{side}_pitcher_stats"))
    innings, strikeouts, walks = _num(stats.get("IP")), _num(stats.get("K")), _num(stats.get("BB"))
    return {
        "name": game.get(f"{side}_pitcher") or game.get(f"{side}_probable_pitcher") or "Starter",
        "era": _num(stats.get("ERA") or stats.get("era")),
        "whip": _num(stats.get("WHIP") or stats.get("whip")),
        "fip": _num(stats.get("FIP") or stats.get("fip")),
        "k_pct": _num(stats.get("K%") or stats.get("k_pct")),
        "bb_pct": _num(stats.get("BB%") or stats.get("bb_pct")),
        "k_per_9": (strikeouts * 9 / innings) if innings and strikeouts is not None else None,
        "bb_per_9": (walks * 9 / innings) if innings and walks is not None else None,
        "recent_era": _num(_dict(stats.get("recent") or stats.get("recent_form")).get("ERA")),
        "split_era": _num(_dict(stats.get("home_split" if side == "home" else "away_split")).get("ERA")),
    }


def _offense_facts(game: dict[str, Any], side: str, savant: dict[str, Any]) -> dict[str, Any]:
    team = _dict(savant.get(f"{side}_team")) or _dict(_dict(game.get("savant")).get(f"{side}_team"))
    stats = _dict(game.get(f"{side}_team_stats"))
    return {
        "ops": _num(team.get("OPS") or stats.get("OPS")),
        "wrc_plus": _num(team.get("wRC+") or stats.get("wRC+")),
        "iso": _num(team.get("ISO") or stats.get("ISO")),
        "xwoba": _num(team.get("xwOBA") or stats.get("xwOBA")),
        "k_pct": _num(team.get("K%") or stats.get("K%")),
    }


def _bullpen_facts(game: dict[str, Any], side: str, override: dict[str, Any]) -> dict[str, Any]:
    stats = _dict(override.get(side)) or _dict(game.get(f"{side}_bullpen")) or _dict(_dict(game.get("bullpen")).get(side))
    return {
        "era": _num(stats.get("ERA")), "whip": _num(stats.get("WHIP")),
        "recent_workload": _num(stats.get("innings_last_3_days") or stats.get("recent_workload")),
        "closer_available": stats.get("closer_available"),
    }


def _pitcher_score(game: dict[str, Any], side: str, override: dict[str, Any]) -> tuple[float, str]:
    stats = _dict(override.get(side)) or _dict(game.get(f"{side}_pitcher_stats"))
    innings = _num(stats.get("IP"))
    hits = _num(stats.get("H"))
    homers = _num(stats.get("HR"))
    walks = _num(stats.get("BB"))
    strikeouts = _num(stats.get("K"))
    score = 50.0
    score += _metric(stats.get("ERA") or stats.get("era"), 4.15, 7.0, lower=True)
    score += _metric(stats.get("WHIP") or stats.get("whip"), 1.30, 28.0, lower=True)
    score += _metric(stats.get("FIP") or stats.get("fip"), 4.10, 5.0, lower=True)
    if innings and innings > 0:
        score += _metric((hits or 0) * 9 / innings, 8.5, 2.5, lower=True)
        score += _metric((homers or 0) * 9 / innings, 1.15, 8.0, lower=True)
        if strikeouts is not None and walks is not None:
            score += _metric((strikeouts - walks) / innings * 9, 6.0, 2.2)
    recent = _dict(stats.get("recent") or stats.get("recent_form"))
    score += _metric(recent.get("ERA"), 4.15, 2.5, lower=True)
    split = _dict(stats.get("home_split" if side == "home" else "away_split"))
    score += _metric(split.get("ERA"), 4.15, 2.0, lower=True)
    name = game.get(f"{side}_pitcher") or game.get(f"{side}_probable_pitcher") or "Starter"
    facts = _pitcher_facts(game, side, override)
    details = []
    if facts["era"] is not None: details.append(f"{facts['era']:.2f} ERA")
    if facts["whip"] is not None: details.append(f"{facts['whip']:.2f} WHIP")
    if facts["k_pct"] is not None: details.append(f"{facts['k_pct']:.1f}% K rate")
    elif facts["k_per_9"] is not None: details.append(f"{facts['k_per_9']:.1f} K/9")
    if facts["bb_pct"] is not None: details.append(f"{facts['bb_pct']:.1f}% BB rate")
    return _clamp(score), f"{name}: {', '.join(details) if details else 'starter data available, but detailed split fields incomplete'}"


def _offense_score(game: dict[str, Any], side: str, savant: dict[str, Any]) -> tuple[float, str]:
    team = _dict(savant.get(f"{side}_team")) or _dict(_dict(game.get("savant")).get(f"{side}_team"))
    stats = _dict(game.get(f"{side}_team_stats"))
    score = 50.0
    score += _metric(team.get("OPS") or stats.get("OPS"), .720, 80.0)
    score += _metric(team.get("wRC+") or stats.get("wRC+"), 100, .45)
    score += _metric(team.get("ISO") or stats.get("ISO"), .165, 100.0)
    score += _metric(team.get("xwOBA") or stats.get("xwOBA"), .315, 120.0)
    score += _metric(team.get("K%") or stats.get("K%"), 22.5, 1.0, lower=True)
    recent = _dict(game.get(f"{side}_recent_form"))
    score += _metric(recent.get("runs_per_game") or recent.get("runs"), 4.4, 2.5)
    return _clamp(score), "OPS/handedness, contact quality, power, strikeout rate, and recent scoring"


def _bullpen_score(game: dict[str, Any], side: str, override: dict[str, Any]) -> tuple[float, str]:
    stats = _dict(override.get(side)) or _dict(game.get(f"{side}_bullpen")) or _dict(_dict(game.get("bullpen")).get(side))
    score = 50.0
    score += _metric(stats.get("ERA"), 4.10, 6.0, lower=True)
    score += _metric(stats.get("WHIP"), 1.30, 24.0, lower=True)
    score += _metric(stats.get("innings_last_3_days") or stats.get("recent_workload"), 8.0, 1.8, lower=True)
    if stats.get("closer_available") is False:
        score -= 10
    return _clamp(score), "Bullpen ERA/WHIP, recent workload, leverage usage, and closer availability"


def _recent_score(game: dict[str, Any], side: str) -> tuple[float, str]:
    recent = _dict(game.get(f"{side}_recent_form"))
    wins = _num(recent.get("wins") or game.get(f"{side}_last_10_wins"))
    score = 50.0 if wins is None else 35.0 + wins * 3.0
    return _clamp(score), f"Last-10 wins: {int(wins) if wins is not None else 'unavailable'}"


def _weather_edge(game: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    weather = override or _dict(game.get("weather"))
    score = 50.0
    temp = _num(weather.get("temperature_f") or weather.get("temperature"))
    wind = _num(weather.get("wind_speed_mph") or weather.get("wind_speed"))
    precip = _num(weather.get("precipitation_probability_pct") or weather.get("precipitation_probability"))
    park = str(game.get("park_factor") or "neutral").lower()
    if temp is not None:
        score += 7 if temp >= 80 else -5 if temp <= 55 else 0
    if wind is not None and wind >= 10:
        score += 7 if "out" in str(weather.get("wind_direction") or "").lower() else -4 if "in" in str(weather.get("wind_direction") or "").lower() else 2
    score += 7 if any(x in park for x in ("hitter", "boost", "hr-friendly")) else -7 if any(x in park for x in ("pitcher", "suppress")) else 0
    if precip is not None and precip >= 45:
        score -= 8
    score = _clamp(score)
    side = "over" if score >= 58 else "under" if score <= 42 else "neutral"
    return {"side": side, "score": score, "reason": f"Park={park}; temperature={temp}; wind={wind}; precipitation={precip}"}


def _dk_rows(game: dict[str, Any], override: dict[str, Any]) -> list[dict[str, Any]]:
    rows = override.get("best_available_prices") if isinstance(override.get("best_available_prices"), list) else game.get("best_available_prices")
    return [row for row in rows or [] if isinstance(row, dict) and str(row.get("bookmaker_key") or row.get("bookmaker") or "").lower() == "draftkings"]


def _market_row(rows: list[dict[str, Any]], market: str, team: str, direction: str | None = None) -> dict[str, Any] | None:
    from api.sharp_client import _normalize_team
    aliases = {"moneyline": "h2h", "f5_moneyline": "f5_h2h", "runline": "spreads", "game_total": "totals", "team_total": "team_totals"}
    target = _normalize_team(team)
    for row in rows:
        if str(row.get("market") or "").lower() != aliases[market]:
            continue
        outcome = str(row.get("outcome") or row.get("description") or "")
        if direction and not outcome.lower().startswith(direction.lower()):
            continue
        if market not in {"game_total"} and target not in _normalize_team(outcome):
            continue
        if row.get("price") is None and row.get("odds_american") is None:
            continue
        if market in {"runline", "game_total", "team_total"} and row.get("point") is None:
            continue
        return row
    return None


def _winner(away: float, home: float, away_team: str, home_team: str) -> tuple[str, float]:
    return (home_team, home) if home >= away else (away_team, away)


def _weight(name: str, default: float) -> float:
    return max(0.0, _num(os.getenv(name)) or default)


def build_game_edge_reports(
    slate: list[dict], market_context: dict | None = None,
    weather_context: dict | None = None, lineup_context: dict | None = None,
    bullpen_context: dict | None = None, savant_context: dict | None = None,
) -> list[dict]:
    """Build one structured, non-Telegram edge report per MLB game."""
    weights = {
        "sp": _weight("MLB_EDGE_WEIGHT_SP", 25), "offense": _weight("MLB_EDGE_WEIGHT_OFFENSE", 18),
        "bullpen": _weight("MLB_EDGE_WEIGHT_BULLPEN", 15), "recent": _weight("MLB_EDGE_WEIGHT_RECENT_FORM", 12),
        "weather": _weight("MLB_EDGE_WEIGHT_WEATHER", 10), "market": _weight("MLB_EDGE_WEIGHT_MARKET", 10),
        "h2h": _weight("MLB_EDGE_WEIGHT_H2H", 5), "situational": _weight("MLB_EDGE_WEIGHT_SITUATIONAL", 5),
    }
    reports: list[dict[str, Any]] = []
    for game in slate:
        away, home = str(game.get("away_team") or ""), str(game.get("home_team") or "")
        pitcher_override = _context_for(savant_context, game)
        bullpen_override = _context_for(bullpen_context, game)
        away_sp, away_sp_reason = _pitcher_score(game, "away", pitcher_override)
        home_sp, home_sp_reason = _pitcher_score(game, "home", pitcher_override)
        away_off, _ = _offense_score(game, "away", pitcher_override)
        home_off, _ = _offense_score(game, "home", pitcher_override)
        away_bp, _ = _bullpen_score(game, "away", bullpen_override)
        home_bp, _ = _bullpen_score(game, "home", bullpen_override)
        away_recent, _ = _recent_score(game, "away")
        home_recent, _ = _recent_score(game, "home")
        sp_team, sp_score = _winner(away_sp, home_sp, away, home)
        off_team, off_score = _winner(away_off, home_off, away, home)
        bp_team, bp_score = _winner(away_bp, home_bp, away, home)
        recent_team, recent_score = _winner(away_recent, home_recent, away, home)
        h2h = _dict(game.get("head_to_head") or game.get("h2h"))
        away_h2h, home_h2h = _num(h2h.get("away_wins")) or 0, _num(h2h.get("home_wins")) or 0
        h2h_team = home if home_h2h >= away_h2h else away
        h2h_score = _clamp(50 + abs(home_h2h - away_h2h) * 2)
        weather = _weather_edge(game, _context_for(weather_context, game))
        situational_home = _clamp(52 + ((_num(game.get("home_days_rest")) or 0) - (_num(game.get("away_days_rest")) or 0)) * 2)
        rows = _dk_rows(game, _context_for(market_context, game))

        lineup = _context_for(lineup_context, game) or _dict(game.get("lineups"))
        lineup_text = str(game.get("lineups") or lineup.get("status") or "").lower()
        lineup_confirmed = bool(lineup) and ("confirmed" in lineup_text or lineup.get("confirmed") is True)
        savant_data = _dict(game.get("savant")) or pitcher_override
        team_stats_available = any(
            isinstance(game.get(key), dict) and bool(game.get(key))
            for key in ("away_team_stats", "home_team_stats")
        ) or any(isinstance(savant_data.get(key), dict) and bool(savant_data.get(key)) for key in ("away_team", "home_team"))
        bullpen_available = any(
            any(value is not None for value in _bullpen_facts(game, side, bullpen_override).values())
            for side in ("away", "home")
        )
        data_available = {
            "mlb_stats": bool(game.get("game_pk") or game.get("game_id")) and bool(away and home),
            "sharp_dk": bool(rows), "savant": bool(savant_data),
            "team_stats": team_stats_available, "bullpen": bullpen_available,
            "weather": isinstance(game.get("weather"), dict) and bool(game.get("weather")),
            "lineup": lineup_confirmed,
        }

        # Rank the side by comparative advantages. Neutral/missing components
        # contribute zero rather than forcing a pass.
        directional_weight = weights["sp"] + weights["offense"] + weights["bullpen"] + weights["recent"] + weights["h2h"] + weights["situational"] or 1.0
        net_home = (
            (home_sp - away_sp) * weights["sp"]
            + (home_off - away_off) * weights["offense"]
            + (home_bp - away_bp) * weights["bullpen"]
            + (home_recent - away_recent) * weights["recent"]
            + ((h2h_score - 50) if h2h_team == home else -(h2h_score - 50)) * weights["h2h"]
            + (situational_home - 50) * weights["situational"]
        ) / directional_weight
        selected_team = home if net_home >= 0 else away
        selected_side = "home" if selected_team == home else "away"
        selected_sp = home_sp if selected_side == "home" else away_sp
        selected_bp = home_bp if selected_side == "home" else away_bp
        selected_off = home_off if selected_side == "home" else away_off
        opponent_sp = away_sp if selected_side == "home" else home_sp
        opponent_bp = away_bp if selected_side == "home" else home_bp
        gaps = {"sp": abs(home_sp - away_sp), "offense": abs(home_off - away_off), "bullpen": abs(home_bp - away_bp), "recent": abs(home_recent - away_recent)}
        supporting = [
            team for team, gap in ((sp_team, gaps["sp"]), (off_team, gaps["offense"]), (bp_team, gaps["bullpen"]))
            if gap >= 3
        ]
        consensus = sum(team == selected_team for team in supporting)
        quant = _dict(game.get("betgptai_quant_v21") or game.get("betgptai_quant_v20") or game.get("betgptai_internal"))
        quant_score = _num(quant.get("final_edge_score")) or 50.0
        missing_penalty = sum(not data_available[key] for key in ("savant", "team_stats", "bullpen", "weather")) * 1.25
        overall = _clamp(
            55 + abs(net_home) * 1.2 + max(0, consensus - 1) * 3
            + max(-3, min(6, (quant_score - 50) * .20))
            - missing_penalty - (0 if lineup_confirmed else 2)
        )
        total_direction = "over" if away_sp <= 46 and home_sp <= 46 and weather["side"] == "over" else "under" if away_sp >= 62 and home_sp >= 62 and weather["side"] == "under" else None
        if total_direction == "over":
            total_confidence = 55 + max(0, 50 - away_sp) * .30 + max(0, 50 - home_sp) * .30 + max(0, weather["score"] - 50) * .30
            overall = max(overall, _clamp(total_confidence))
        elif total_direction == "under":
            total_confidence = 55 + max(0, away_sp - 55) * .25 + max(0, home_sp - 55) * .25 + max(0, 50 - weather["score"]) * .30
            overall = max(overall, _clamp(total_confidence))
        available = {
            "moneyline": _market_row(rows, "moneyline", selected_team),
            "f5_moneyline": _market_row(rows, "f5_moneyline", selected_team),
            "runline": _market_row(rows, "runline", selected_team),
            "game_total": _market_row(rows, "game_total", selected_team, total_direction) if total_direction else None,
            "team_total": _market_row(rows, "team_total", selected_team),
        }
        available_markets = [market for market, row in available.items() if row]
        all_markets = ["moneyline", "f5_moneyline", "runline", "game_total", "team_total"]
        missing_markets = [market for market in all_markets if market not in available_markets]
        red_flags: list[str] = []
        if not lineup_confirmed: red_flags.append("lineup_not_confirmed")
        if weather["score"] <= 35: red_flags.append("weather_risk")
        if not any(available.values()): red_flags.append("market_not_verified")
        if abs(away_sp - home_sp) < 4: red_flags.append("starter_volatility")
        if bp_team != selected_team and abs(away_bp - home_bp) >= 8: red_flags.append("bullpen_conflict")
        if supporting and consensus < max(1, len(supporting) // 2): red_flags.append("conflicting_signals")
        if overall < 55: red_flags.append("low_edge")

        best_market = "pass"
        strongest_factor = max(gaps, key=gaps.get)
        if overall >= 55 and weather["score"] > 30 and available_markets:
            if total_direction and available["game_total"]:
                best_market = "game_total"
            elif strongest_factor == "sp" and sp_team == selected_team and available["f5_moneyline"]:
                best_market = "f5_moneyline"
            elif sp_team == selected_team and bp_team == selected_team and available["moneyline"]:
                best_market = "moneyline"
            elif strongest_factor == "offense" and off_team == selected_team and opponent_bp <= 47 and available["runline"]:
                best_market = "runline"
            elif strongest_factor == "offense" and off_team == selected_team and available["team_total"]:
                best_market = "team_total"
            elif available["moneyline"]:
                best_market = "moneyline"
            elif available["f5_moneyline"]:
                best_market = "f5_moneyline"
            elif available["team_total"]:
                best_market = "team_total"
            elif available["runline"]:
                best_market = "runline"
            elif available["game_total"]:
                best_market = "game_total"
        row = available.get(best_market) if best_market != "pass" else None
        odds = row.get("price") if row and row.get("price") is not None else row.get("odds_american") if row else None
        line = row.get("point") if row else None
        selection = str(row.get("outcome") or row.get("description") or selected_team) if row else selected_team
        market_verified = bool(row and odds is not None and (best_market not in {"runline", "game_total", "team_total"} or line is not None))
        market_score = 70.0 if market_verified or available_markets else 35.0
        thresholds = {"moneyline": 65, "f5_moneyline": 62, "runline": 65, "game_total": 62, "team_total": 62}
        qualified = bool(best_market != "pass" and overall >= thresholds.get(best_market, 65))
        watchlist = bool(best_market != "pass" and not qualified and overall >= 55)
        qualification_status = "qualified" if qualified else "watchlist" if watchlist else "pass"
        pass_reason = None
        if best_market == "pass":
            if not available_markets: pass_reason = "No usable verified DraftKings market was available."
            elif overall < 55: pass_reason = "No side produced a clear statistical edge above the minimum confidence floor."
            elif weather["score"] <= 30: pass_reason = "Weather creates major uncertainty."
            else: pass_reason = "SP, offense, and bullpen signals conflict too severely."
        watch_market = best_market if best_market != "pass" else ("f5_moneyline" if sp_team == selected_team and available.get("f5_moneyline") else "moneyline")
        market_lines = []
        for market_name, market_row in available.items():
            if not market_row:
                continue
            market_lines.append({
                "market": market_name,
                "selection": market_row.get("outcome") or market_row.get("description"),
                "line": market_row.get("point"),
                "odds_american": market_row.get("price") if market_row.get("price") is not None else market_row.get("odds_american"),
            })
        per_market_value = {
            market: {"available": bool(available.get(market)), "score": 70.0 if available.get(market) else None}
            for market in all_markets
        }
        key_reason = (
            f"Starter edge favors {sp_team}; offense favors {off_team}; bullpen favors {bp_team}."
        )
        reports.append({
            "game_id": game.get("game_id") or game.get("game_pk"), "game_pk": game.get("game_pk") or game.get("game_id"),
            "away_team": away, "home_team": home, "start_time": game.get("game_time") or game.get("start_time"),
            "sp_edge": {"team": sp_team, "score": sp_score, "reason": f"{away_sp_reason}; {home_sp_reason}", "away": _pitcher_facts(game, "away", pitcher_override), "home": _pitcher_facts(game, "home", pitcher_override)},
            "offense_edge": {"team": off_team, "score": off_score, "reason": "OPS vs handedness, wRC+, ISO, K%, Statcast, recent scoring, and top-lineup strength", "away": _offense_facts(game, "away", pitcher_override), "home": _offense_facts(game, "home", pitcher_override)},
            "bullpen_edge": {"team": bp_team, "score": bp_score, "reason": "ERA/WHIP, workload, leverage usage, closer availability, and mismatch", "away": _bullpen_facts(game, "away", bullpen_override), "home": _bullpen_facts(game, "home", bullpen_override)},
            "weather_park_edge": weather,
            "recent_form_edge": {"team": recent_team, "score": recent_score, "reason": "Recent wins and scoring form"},
            "h2h_trend_edge": {"team": h2h_team, "score": h2h_score, "reason": "Small-weight head-to-head context"},
            "market_value": {"market": best_market, "score": market_score, "sportsbook": "draftkings", "line_verified": market_verified, "reference_lines_verified": bool(market_lines), "reason": "Verified DraftKings reference" if market_verified else "Other usable DraftKings markets are available" if market_lines else "No matching verified DraftKings row", "available_lines": market_lines, "per_market": per_market_value},
            "available_markets": available_markets, "missing_markets": missing_markets,
            "data_available": data_available,
            "best_market": best_market,
            "official_pick_candidate": {"market_type": best_market, "team": selected_team, "selection": selection, "line": line, "odds_american": odds, "confidence": overall, "edge_score": overall, "source": SOURCE, "qualified": qualified, "watchlist_only": watchlist},
            "overall_edge_score": overall, "confidence_grade": "Elite" if overall >= 85 else "Strong" if overall >= 75 else "Lean" if overall >= 55 else "Pass",
            "key_reason": key_reason, "red_flags": red_flags, "pass_reason": pass_reason,
            "watchlist": watchlist, "watch_market": watch_market, "qualification_status": qualification_status,
        })
    return sorted(reports, key=lambda report: float(report.get("overall_edge_score") or 0), reverse=True)


_MARKET_LABELS = {
    "moneyline": "ML", "f5_moneyline": "F5 ML", "runline": "RL",
    "team_total": "Team Total", "game_total": "Game Total", "pass": "Pass",
}
_FLAG_LABELS = {
    "lineup_not_confirmed": "Lineup not confirmed",
    "low_edge": "No clear model edge",
    "market_not_verified": "DraftKings line not verified",
    "starter_volatility": "Starter volatility",
    "bullpen_conflict": "Conflicting bullpen signal",
    "conflicting_signals": "Conflicting SP/offense/bullpen signals",
    "weather_risk": "Weather risk",
}


def _report_date(card_date: str | None, reports: list[dict[str, Any]]) -> str:
    value = card_date or (str(reports[0].get("start_time") or "")[:10] if reports else "")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%m/%d/%Y")
    except ValueError:
        return value or "Date unavailable"


def _format_sp_edge_text(report: dict[str, Any]) -> str:
    edge = _dict(report.get("sp_edge"))
    team = str(edge.get("team") or "Team")
    side = "away" if team == report.get("away_team") else "home"
    facts = _dict(edge.get(side))
    pieces = []
    if facts.get("era") is not None: pieces.append(f"{facts['era']:.2f} ERA")
    if facts.get("whip") is not None: pieces.append(f"{facts['whip']:.2f} WHIP")
    if facts.get("k_pct") is not None: pieces.append(f"{facts['k_pct']:.1f}% K rate")
    elif facts.get("k_per_9") is not None: pieces.append(f"{facts['k_per_9']:.1f} K/9")
    if facts.get("bb_pct") is not None: pieces.append(f"{facts['bb_pct']:.1f}% BB rate")
    pitcher = facts.get("name") or "Starter"
    if pieces:
        return f"{pitcher} owns the cleaner {', '.join(pieces)} profile."
    return f"{pitcher} has the better starter profile, but detailed split fields are incomplete."


def _format_offense_edge_text(report: dict[str, Any]) -> str:
    edge = _dict(report.get("offense_edge"))
    team = str(edge.get("team") or "One offense")
    side = "away" if team == report.get("away_team") else "home"
    facts = _dict(edge.get(side))
    pieces = []
    if facts.get("ops") is not None: pieces.append(f"{facts['ops']:.3f} OPS")
    if facts.get("wrc_plus") is not None: pieces.append(f"{facts['wrc_plus']:.0f} wRC+")
    if facts.get("xwoba") is not None: pieces.append(f"{facts['xwoba']:.3f} xwOBA")
    if not pieces:
        return "Detailed offense splits are incomplete; no decisive offense edge is confirmed."
    return f"{team} rates better in {', '.join(pieces)}."


def _format_bullpen_edge_text(report: dict[str, Any]) -> str:
    edge = _dict(report.get("bullpen_edge"))
    team = str(edge.get("team") or "One bullpen")
    side = "away" if team == report.get("away_team") else "home"
    facts = _dict(edge.get(side))
    pieces = []
    if facts.get("era") is not None: pieces.append(f"{facts['era']:.2f} ERA")
    if facts.get("whip") is not None: pieces.append(f"{facts['whip']:.2f} WHIP")
    if not pieces:
        return "Bullpen detail is limited; no decisive relief edge is confirmed."
    return f"{team} has the stronger bullpen profile ({', '.join(pieces)})."


def _format_market_text(report: dict[str, Any]) -> str:
    market = _dict(report.get("market_value"))
    lines = market.get("available_lines") if isinstance(market.get("available_lines"), list) else []
    formatted = []
    for row in lines[:4]:
        odds = _num(row.get("odds_american"))
        odds_text = f" ({int(odds):+d})" if odds is not None else ""
        label = _MARKET_LABELS.get(str(row.get("market")), str(row.get("market") or "Market"))
        point = f" {float(row['line']):+g}" if row.get("line") is not None and row.get("market") == "runline" else ""
        formatted.append(f"{row.get('selection')} {label}{point}{odds_text}")
    verified = "verified" if market.get("reference_lines_verified") else "not verified"
    return f"DraftKings line: {verified}. Best available: {', '.join(formatted) if formatted else 'No matching reference line'}."


def _format_watchouts(report: dict[str, Any]) -> str:
    return "; ".join(_FLAG_LABELS.get(flag, str(flag).replace("_", " ").title()) for flag in report.get("red_flags") or []) or "No major watchouts"


def _format_data_used(report: dict[str, Any]) -> str:
    data = _dict(report.get("data_available"))
    labels = (("mlb_stats", "MLB"), ("sharp_dk", "DK"), ("savant", "Savant"), ("team_stats", "team stats"), ("bullpen", "bullpen"), ("weather", "weather"), ("lineup", "lineup"))
    return ", ".join(label for key, label in labels if data.get(key)) or "MLB schedule only"


def _format_decision_text(report: dict[str, Any]) -> str:
    candidate = _dict(report.get("official_pick_candidate"))
    market = str(report.get("best_market") or "pass")
    if market == "pass":
        return f"PASS — {report.get('pass_reason') or 'signals do not align clearly enough.'}"
    if report.get("qualification_status") == "watchlist":
        return f"👀 {candidate.get('team')} {_MARKET_LABELS.get(market, market.title())} Watch — {_format_watchouts(report)}"
    return f"✅ {candidate.get('team')} {_MARKET_LABELS.get(market, market.title())} — {report.get('key_reason')}"


def _format_summary_why(report: dict[str, Any]) -> str:
    candidate = _dict(report.get("official_pick_candidate"))
    team = candidate.get("team")
    data = _dict(report.get("data_available"))
    reasons = []
    if _dict(report.get("sp_edge")).get("team") == team:
        reasons.append("SP edge")
    if data.get("team_stats") and _dict(report.get("offense_edge")).get("team") == team:
        reasons.append("offense vs handedness")
    if data.get("bullpen") and _dict(report.get("bullpen_edge")).get("team") == team:
        reasons.append("bullpen support")
    if _dict(report.get("market_value")).get("line_verified"):
        reasons.append("DK line verified")
    return " + ".join(reasons) or "Best available API signals support the lean"


def render_game_edge_debug(reports: list[dict[str, Any]], card_date: str | None = None) -> str:
    lines = ["🧪 MLB GAME EDGE REPORT", f"📅 {_report_date(card_date, reports)}", f"Reports: {len(reports)}"]
    for report in reports:
        candidate = _dict(report.get("official_pick_candidate"))
        best_market = str(report.get("best_market") or "pass")
        lean_market = report.get("watch_market") if best_market == "pass" else best_market
        lines.extend([
            "", "━━━━━━━━━━━━━━",
            f"{report.get('away_team')} @ {report.get('home_team')}",
            f"Best Lean: {candidate.get('team')} {_MARKET_LABELS.get(str(lean_market), 'ML')} | Best Market: {_MARKET_LABELS.get(best_market, best_market.title())}",
            f"Data Used: {_format_data_used(report)}",
            f"• SP edge: {_format_sp_edge_text(report)}",
            f"• Offense: {_format_offense_edge_text(report)}",
            f"• Bullpen: {_format_bullpen_edge_text(report)}",
            f"Market: {_format_market_text(report)}",
            f"Watchouts: {_format_watchouts(report)}",
            f"Decision: {_format_decision_text(report)}",
        ])
    return "\n".join(lines).strip()


def render_game_edge_summary(reports: list[dict[str, Any]], card_date: str | None = None) -> str:
    qualified = [report for report in reports if report.get("qualification_status") == "qualified"]
    watchlist = [report for report in reports if report.get("qualification_status") == "watchlist"]
    passed = [report for report in reports if report.get("qualification_status") == "pass"]
    lines = ["⚾ MLB EDGE SUMMARY", f"📅 {_report_date(card_date, reports)}", "", f"✅ Qualified Picks: {len(qualified)}"]
    if qualified:
        for index, report in enumerate(qualified[:10], start=1):
            candidate = _dict(report.get("official_pick_candidate"))
            lines.extend([f"{index}. {candidate.get('team')} — {_MARKET_LABELS.get(str(report.get('best_market')), 'Pick')}", f"   Why: {_format_summary_why(report)}."])
    else:
        lines.append("- None")
    lines.extend(["", f"👀 Watchlist: {len(watchlist)}"])
    if watchlist:
        for index, report in enumerate(watchlist[:10], start=1):
            candidate = _dict(report.get("official_pick_candidate"))
            lines.extend([f"{index}. {candidate.get('team')} — {_MARKET_LABELS.get(str(report.get('watch_market')), 'ML')} Watch", f"   Why: {_format_watchouts(report)}."])
    else:
        lines.append("- None")
    reason_counts: dict[str, int] = {}
    for report in passed:
        for flag in report.get("red_flags") or []:
            reason_counts[flag] = reason_counts.get(flag, 0) + 1
    lines.extend(["", "🚫 Passed Games", f"Passes: {len(passed)}", "Main reasons:"])
    for flag, _ in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:3]:
        lines.append(f"• {_FLAG_LABELS.get(flag, flag.replace('_', ' ').title())}")
    if not reason_counts: lines.append("• None")
    data_counts = {
        key: sum(1 for report in reports if _dict(report.get("data_available")).get(key))
        for key in ("mlb_stats", "sharp_dk", "savant", "weather", "bullpen")
    }
    lines.extend([
        "", "Main data used:",
        f"MLB Stats API: {'yes' if data_counts['mlb_stats'] else 'no'}",
        f"Sharp DK: {'yes' if data_counts['sharp_dk'] else 'no'}, matched games {data_counts['sharp_dk']}",
        f"Savant: {'yes' if data_counts['savant'] else 'no'}",
        f"Weather: {'yes' if data_counts['weather'] else 'no'}",
        f"Bullpen: {'yes' if data_counts['bullpen'] else 'no'}",
    ])
    return "\n".join(lines).strip()


def render_game_edge_data_debug(reports: list[dict[str, Any]], card_date: str | None = None) -> str:
    lines = ["🧬 MLB GAME EDGE DATA DEBUG", f"📅 {_report_date(card_date, reports)}", f"Games: {len(reports)}"]
    for report in reports:
        data = _dict(report.get("data_available"))
        lines.extend([
            "", f"- {report.get('away_team')} @ {report.get('home_team')}",
            f"  MLB stats: {'YES' if data.get('mlb_stats') else 'NO'} | DK market: {'YES' if data.get('sharp_dk') else 'NO'} | Savant: {'YES' if data.get('savant') else 'NO'}",
            f"  Team stats: {'YES' if data.get('team_stats') else 'NO'} | Bullpen: {'YES' if data.get('bullpen') else 'NO'} | Weather: {'YES' if data.get('weather') else 'NO'} | Lineup: {'YES' if data.get('lineup') else 'NO'}",
            f"  Available DK markets: {', '.join(report.get('available_markets') or []) or 'none'}",
            f"  Best market: {_MARKET_LABELS.get(str(report.get('best_market')), str(report.get('best_market')).title())} | Status: {report.get('qualification_status')}",
        ])
    return "\n".join(lines).strip()
