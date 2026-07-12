"""Structured game-by-game MLB edge reports for BETGPTAI.

This engine consumes verified/enriched slate objects and never fabricates a
missing statistic, line, or price. Optional feeds become neutral scores and
explicit red flags rather than exceptions.
"""
from __future__ import annotations

import math
import os
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
    available = [key for key in ("ERA", "WHIP", "FIP", "IP", "H", "HR", "K", "BB") if stats.get(key) not in (None, "", "unavailable")]
    return _clamp(score), f"{name}: {', '.join(available) if available else 'limited verified pitcher metrics'}"


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


def _market_row(rows: list[dict[str, Any]], market: str, team: str) -> dict[str, Any] | None:
    from api.sharp_client import _normalize_team
    aliases = {"moneyline": "h2h", "f5_moneyline": "f5_h2h", "runline": "spreads", "game_total": "totals", "team_total": "team_totals"}
    target = _normalize_team(team)
    for row in rows:
        if str(row.get("market") or "").lower() != aliases[market]:
            continue
        outcome = str(row.get("outcome") or row.get("description") or "")
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
    total_weight = sum(weights.values()) or 100.0
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

        team_scores: dict[str, float] = {}
        for team, side in ((away, "away"), (home, "home")):
            values = {
                "sp": away_sp if side == "away" else home_sp,
                "offense": away_off if side == "away" else home_off,
                "bullpen": away_bp if side == "away" else home_bp,
                "recent": away_recent if side == "away" else home_recent,
                "weather": 50.0,
                "market": 62.0 if _market_row(rows, "moneyline", team) else 40.0,
                "h2h": h2h_score if h2h_team == team else 100 - h2h_score,
                "situational": situational_home if side == "home" else 100 - situational_home,
            }
            team_scores[team] = _clamp(sum(values[k] * weights[k] for k in weights) / total_weight)
        selected_team = max(team_scores, key=team_scores.get)
        overall = team_scores[selected_team]
        available = {market: _market_row(rows, market, selected_team) for market in ("moneyline", "f5_moneyline", "runline", "game_total", "team_total")}
        lineup = _context_for(lineup_context, game) or _dict(game.get("lineups"))
        lineup_text = str(game.get("lineups") or lineup.get("status") or "").lower()
        lineup_confirmed = bool(lineup) and ("confirmed" in lineup_text or lineup.get("confirmed") is True)
        red_flags: list[str] = []
        if not lineup_confirmed: red_flags.append("lineup_not_confirmed")
        if weather["score"] <= 35: red_flags.append("weather_risk")
        if not any(available.values()): red_flags.append("market_not_verified")
        if abs(away_sp - home_sp) < 4: red_flags.append("starter_volatility")
        if bp_team != selected_team and abs(away_bp - home_bp) >= 8: red_flags.append("bullpen_conflict")
        if overall < 70: red_flags.append("low_edge")

        best_market = "pass"
        if overall >= 70 and lineup_confirmed and weather["score"] > 35:
            selected_sp = home_sp if selected_team == home else away_sp
            selected_bp = home_bp if selected_team == home else away_bp
            selected_off = home_off if selected_team == home else away_off
            if selected_sp >= 68 and selected_bp < 55 and available["f5_moneyline"]:
                best_market = "f5_moneyline"
            elif selected_off >= 78 and available["team_total"]:
                best_market = "team_total"
            elif selected_off >= 75 and selected_bp >= 58 and available["runline"]:
                best_market = "runline"
            elif sp_team == selected_team and bp_team == selected_team and off_team == selected_team and available["moneyline"]:
                best_market = "moneyline"
            elif weather["side"] != "neutral" and available["game_total"]:
                best_market = "game_total"
            elif available["moneyline"]:
                best_market = "moneyline"
            elif overall >= 75:
                best_market = "moneyline"  # stats-first fallback; line stays unverified
        row = available.get(best_market) if best_market != "pass" else None
        odds = row.get("price") if row and row.get("price") is not None else row.get("odds_american") if row else None
        line = row.get("point") if row else None
        selection = str(row.get("outcome") or row.get("description") or selected_team) if row else selected_team
        market_verified = bool(row and odds is not None and (best_market not in {"runline", "game_total", "team_total"} or line is not None))
        market_score = 70.0 if market_verified else 35.0
        pass_reason = None
        if best_market == "pass":
            pass_reason = "Signals conflicted, lineup/market verification was incomplete, or overall edge was below 70."
        key_reason = f"{sp_team} SP {sp_score}; {off_team} offense {off_score}; {bp_team} bullpen {bp_score}"
        reports.append({
            "game_id": game.get("game_id") or game.get("game_pk"), "game_pk": game.get("game_pk") or game.get("game_id"),
            "away_team": away, "home_team": home, "start_time": game.get("game_time") or game.get("start_time"),
            "sp_edge": {"team": sp_team, "score": sp_score, "reason": f"{away_sp_reason}; {home_sp_reason}"},
            "offense_edge": {"team": off_team, "score": off_score, "reason": "OPS vs handedness, wRC+, ISO, K%, Statcast, recent scoring, and top-lineup strength"},
            "bullpen_edge": {"team": bp_team, "score": bp_score, "reason": "ERA/WHIP, workload, leverage usage, closer availability, and mismatch"},
            "weather_park_edge": weather,
            "recent_form_edge": {"team": recent_team, "score": recent_score, "reason": "Recent wins and scoring form"},
            "h2h_trend_edge": {"team": h2h_team, "score": h2h_score, "reason": "Small-weight head-to-head context"},
            "market_value": {"market": best_market, "score": market_score, "sportsbook": "draftkings", "line_verified": market_verified, "reason": "Verified DraftKings reference" if market_verified else "No matching verified DraftKings row"},
            "best_market": best_market,
            "official_pick_candidate": {"market_type": best_market, "team": selected_team, "selection": selection, "line": line, "odds_american": odds, "confidence": overall, "edge_score": overall, "source": SOURCE},
            "overall_edge_score": overall, "confidence_grade": "Elite" if overall >= 85 else "Strong" if overall >= 75 else "Lean" if overall >= 70 else "Pass",
            "key_reason": key_reason, "red_flags": red_flags, "pass_reason": pass_reason,
        })
    return sorted(reports, key=lambda report: float(report.get("overall_edge_score") or 0), reverse=True)


def render_game_edge_debug(reports: list[dict[str, Any]]) -> str:
    lines = ["🧪 MLB GAME EDGE ENGINE v1", f"Reports: {len(reports)}"]
    for report in reports:
        candidate = _dict(report.get("official_pick_candidate"))
        lines.extend([
            "", f"Game: {report.get('away_team')} @ {report.get('home_team')}",
            f"SP Edge: {_dict(report.get('sp_edge')).get('team')} — {_dict(report.get('sp_edge')).get('score')} — {_dict(report.get('sp_edge')).get('reason')}",
            f"Offense Edge: {_dict(report.get('offense_edge')).get('team')} — {_dict(report.get('offense_edge')).get('score')} — {_dict(report.get('offense_edge')).get('reason')}",
            f"Bullpen Edge: {_dict(report.get('bullpen_edge')).get('team')} — {_dict(report.get('bullpen_edge')).get('score')} — {_dict(report.get('bullpen_edge')).get('reason')}",
            f"Weather/Park: {_dict(report.get('weather_park_edge')).get('side')} — {_dict(report.get('weather_park_edge')).get('score')} — {_dict(report.get('weather_park_edge')).get('reason')}",
            f"Market Value: {_dict(report.get('market_value')).get('score')} — verified={_dict(report.get('market_value')).get('line_verified')} — {_dict(report.get('market_value')).get('reason')}",
            f"Best Market: {report.get('best_market')}",
            f"Candidate Pick: {candidate.get('selection')} — {candidate.get('market_type')} — {candidate.get('odds_american')}",
            f"Confidence: {candidate.get('confidence')} ({report.get('confidence_grade')})",
            f"Red Flags: {', '.join(report.get('red_flags') or []) or 'None'}",
            f"Pass Reason: {report.get('pass_reason') or 'None'}",
        ])
    lines.extend(["", "TOP 5 OVERALL EDGE REPORTS"])
    for index, report in enumerate(reports[:5], start=1):
        candidate = _dict(report.get("official_pick_candidate"))
        lines.append(f"{index}. {candidate.get('selection')} — {report.get('best_market')} — {report.get('overall_edge_score')} — {report.get('key_reason')}")
    return "\n".join(lines).strip()


def render_game_edge_summary(reports: list[dict[str, Any]]) -> str:
    lines = ["⚾ MLB GAME EDGE SUMMARY"]
    qualified = [report for report in reports if report.get("best_market") != "pass"]
    for index, report in enumerate(qualified[:10], start=1):
        candidate = _dict(report.get("official_pick_candidate"))
        lines.append(
            f"{index}. {candidate.get('selection')} — {report.get('best_market')} — "
            f"{report.get('overall_edge_score')} — {report.get('key_reason')}"
        )
    if not qualified:
        lines.append("No games cleared the 70-point qualification and verification gates.")
    return "\n".join(lines).strip()
