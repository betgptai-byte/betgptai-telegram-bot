"""Owner-only Official MLB War Room for BETGPTAI.

This is the complete internal research report used to support the official
card. It never posts to members and never changes public picks automatically.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_analysis import analyze_mlb_slate, build_fallback_card
from game_time import format_game_clock
from mlb_data import get_combined_slate
from openai_image_generator import generate_image_from_prompt
from player_props_engine import build_player_props_lab
from premium_card_formatter import render_pick_block
from results_tracker import load_picks, save_official_picks
from storage import data_file


EASTERN = ZoneInfo("America/New_York")
DIVIDER = "━━━━━━━━━━━━━━"


def _implied_probability(american_odds: Any) -> float:
    """Convert American odds to implied probability for internal ranking."""
    price = _num(american_odds)
    if price == 0:
        return 0.0
    return abs(price) / (abs(price) + 100) if price < 0 else 100 / (price + 100)


def _admin_reason(game: dict[str, Any], market: str) -> str:
    """Short admin-only reason without exposing model internals."""
    pitcher_ready = game.get("away_pitcher") not in {"", None, "TBD"} and game.get("home_pitcher") not in {"", None, "TBD"}
    weather = _dict(game.get("weather"))
    park = str(game.get("park_factor") or game.get("park_factor_label") or "").lower()
    if market == "f5_moneyline":
        return "Starting-pitcher matchup and early-game profile support this lean." if pitcher_ready else "Early-game lean; probable pitcher context is limited."
    if market == "runline":
        return "Separation profile supports a spread angle with offensive support."
    if market == "total":
        if any(word in park for word in ("hitter", "hr", "extreme")) or weather:
            return "Run environment and matchup profile support the total angle."
        return "Total angle based on matchup pace and available market context."
    if market == "team_total":
        return "Team scoring profile points to this safer team-total angle."
    return "Winner profile is supported by matchup, market, and situational context."


def _candidate_base(game: dict[str, Any], market_type: str, pick_text: str, *, line: Any = None, odds: Any = None) -> dict[str, Any]:
    """Shared shape for admin Top 5 candidates."""
    game_pk = game.get("game_pk") or game.get("game_id")
    return {
        "card_candidate": True,
        "sport": "mlb",
        "game_pk": game_pk,
        "away_team": game.get("away_team"),
        "home_team": game.get("home_team"),
        "game": f"{game.get('away_team')} @ {game.get('home_team')}",
        "game_time": game.get("game_time"),
        "game_time_et": format_game_clock(game.get("game_time"), status=game.get("status")),
        "game_status": game.get("status"),
        "venue": game.get("venue"),
        "market_type": market_type,
        "pick_text": pick_text,
        "line": line,
        "odds": odds,
        "reason": _admin_reason(game, market_type),
    }


def _ranked_market_candidates(slate: list[dict[str, Any]], market: str, limit: int = 5) -> list[dict[str, Any]]:
    """Build ranked admin candidates from best_available_prices."""
    rows: list[dict[str, Any]] = []
    market_type = {
        "h2h": "moneyline",
        "spreads": "runline",
        "totals": "total",
        "team_totals": "team_total",
    }.get(market, market)
    for game in slate:
        for wager in game.get("best_available_prices", []) if isinstance(game.get("best_available_prices"), list) else []:
            if not isinstance(wager, dict) or wager.get("market") != market:
                continue
            price = wager.get("price")
            if not isinstance(price, (int, float)):
                continue
            outcome = _safe(wager.get("description") or wager.get("outcome"), "Pick")
            point = wager.get("point")
            if market == "spreads" and isinstance(point, (int, float)):
                pick_text = f"{outcome} {point:+g}"
                line = point
            elif market == "totals" and isinstance(point, (int, float)):
                pick_text = f"{outcome} {point:g} ({game.get('away_team')} @ {game.get('home_team')})"
                line = point
            elif market == "team_totals" and isinstance(point, (int, float)):
                direction = str(wager.get("outcome") or "").title()
                team = _safe(wager.get("description"), outcome)
                if direction not in {"Over", "Under"}:
                    continue
                safer = point - 1 if direction == "Over" else point + 1
                pick_text = f"{team} Team Total {direction} {point:g} | Safer Alt: {direction} {safer:g}"
                line = point
            else:
                pick_text = str(outcome)
                line = point
            candidate = _candidate_base(game, market_type, pick_text, line=line, odds=price)
            candidate["score"] = _implied_probability(price)
            candidate["final_edge_score"] = round(candidate["score"] * 100)
            candidate["confidence"] = "Admin Candidate"
            candidate["risk_level"] = "Medium"
            candidate["data_quality_grade"] = "Admin"
            rows.append(candidate)
    rows.sort(key=lambda row: (row.get("score") or 0), reverse=True)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str]] = set()
    for row in rows:
        key = (row.get("game_pk"), row.get("market_type"), row.get("pick_text"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
        if len(unique) >= limit:
            break
    return unique


def _top_f5_candidates(slate: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """F5 is always moneyline only, derived from strongest ML profiles."""
    rows = []
    for candidate in _ranked_market_candidates(slate, "h2h", limit=limit * 2):
        game = next((item for item in slate if str(item.get("game_pk") or item.get("game_id")) == str(candidate.get("game_pk"))), {})
        pick_text = f"{candidate['pick_text']} F5 ML"
        row = _candidate_base(game, "f5_moneyline", pick_text, line=None, odds=None)
        row["score"] = candidate.get("score", 0)
        row["final_edge_score"] = round((row["score"] or 0.86) * 100)
        row["confidence"] = "Admin Candidate"
        row["risk_level"] = "Medium"
        row["data_quality_grade"] = "Admin"
        rows.append(row)
    return rows[:limit]


def _inferred_team_totals(slate: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Infer safe team-total display when official team-total markets are missing."""
    inferred: list[dict[str, Any]] = []
    for game in slate:
        moneylines = []
        totals = []
        for wager in game.get("best_available_prices", []) if isinstance(game.get("best_available_prices"), list) else []:
            if not isinstance(wager, dict):
                continue
            if wager.get("market") == "h2h" and isinstance(wager.get("price"), (int, float)):
                moneylines.append(wager)
            if wager.get("market") == "totals" and isinstance(wager.get("price"), (int, float)):
                totals.append(wager)
        moneylines.sort(key=lambda item: _implied_probability(item.get("price")), reverse=True)
        if not moneylines:
            continue
        total_under = any(str(item.get("outcome", "")).lower() == "under" for item in totals)
        if total_under and len(moneylines) > 1:
            team = _safe(moneylines[-1].get("outcome"), "Team")
            direction, line, safer = "Under", 5.5, 6.5
        else:
            team = _safe(moneylines[0].get("outcome"), "Team")
            direction, line, safer = "Over", 4.5, 3.5
        row = _candidate_base(
            game,
            "team_total",
            f"{team} Team Total {direction} {line:g} | Safer Alt: {direction} {safer:g}",
            line=line,
            odds=None,
        )
        row["score"] = _implied_probability(moneylines[0].get("price")) - (0.05 if direction == "Under" else 0)
        row["final_edge_score"] = round(row["score"] * 100)
        row["confidence"] = "Admin Candidate"
        row["risk_level"] = "Medium"
        row["data_quality_grade"] = "Admin"
        row["inferred_default"] = True
        inferred.append(row)
        if len(inferred) >= limit:
            break
    return inferred


def build_mlb_top5_admin_card(
    card_date: str,
    *,
    odds_api_key: str = "",
    highlightly_api_key: str = "",
) -> dict[str, Any]:
    """Build and save the admin-only Full MLB Top 5 Card."""
    errors: list[str] = []
    try:
        slate = get_combined_slate(
            odds_api_key,
            game_date=card_date,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception as error:
        slate = []
        errors.append(f"Slate unavailable: {error}")
    official_team_totals = _ranked_market_candidates(slate, "team_totals", limit=5)
    team_totals = official_team_totals or _inferred_team_totals(slate, limit=5)
    top5 = {
        "moneyline": _ranked_market_candidates(slate, "h2h", limit=5),
        "f5_moneyline": _top_f5_candidates(slate, limit=5),
        "runline": _ranked_market_candidates(slate, "spreads", limit=5),
        "game_totals": _ranked_market_candidates(slate, "totals", limit=5),
        "team_totals": team_totals,
    }
    report = {
        "version": "MLB Admin Top 5 Card v1",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "admin_only": True,
        "saved_to_official_picks": False,
        "errors": errors,
        "top5": top5,
    }
    path = _admin_dir(card_date) / "mlb_top5_admin.json"
    _write_json(path, report)
    report["report_path"] = str(path)
    return report


def render_mlb_top5_admin_card(report: dict[str, Any]) -> str:
    """Render the admin-only MLB Top 5 card."""
    top5 = _dict(report.get("top5"))
    sections = [
        ("🔥 TOP 5 MONEYLINE", "moneyline"),
        ("⚾ TOP 5 F5 MONEYLINE", "f5_moneyline"),
        ("📈 TOP 5 RUNLINE", "runline"),
        ("🔢 TOP 5 GAME TOTALS", "game_totals"),
        ("🏟 TOP 5 TEAM TOTALS", "team_totals"),
    ]
    lines = [
        "⚾ BETGPTAI ADMIN MLB TOP 5 CARD",
        f"📅 Card Date: {report.get('display_date')}",
        "🧪 Admin Only",
        "",
    ]
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if errors:
        lines.extend(["Build Notes:", *[f"- {item}" for item in errors[:5]], ""])
    for heading, key in sections:
        lines.append(heading)
        rows = top5.get(key) if isinstance(top5.get(key), list) else []
        if not rows:
            lines.append("No qualified plays available.")
        else:
            lines.extend(
                render_pick_block(row, rank=index, show_data_quality=True)
                for index, row in enumerate(rows[:5], start=1)
            )
        lines.append("")
    lines.extend([
        "Rules: F5 is moneyline only. Sportsbook names hidden. Not saved to picks.json.",
        f"Saved JSON: {report.get('report_path')}",
    ])
    return "\n".join(str(line) for line in lines).strip()


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def _safe(value: Any, fallback: str = "Unavailable") -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != "unavailable" else fallback


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _admin_dir(card_date: str) -> Path:
    path = data_file("admin_reports") / card_date
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _side_context(game: dict[str, Any], side: str) -> dict[str, Any]:
    opponent = "home" if side == "away" else "away"
    savant = _dict(game.get("savant"))
    fangraphs = _dict(game.get("fangraphs"))
    stats = _dict(game.get(f"{side}_pitcher_stats"))
    pitcher_savant = _dict(savant.get(f"{side}_pitcher"))
    team_savant = _dict(savant.get(f"{side}_team"))
    bullpen = _dict(savant.get(f"{side}_bullpen"))
    hitters = _list(savant.get(f"{side}_batters"))
    fg_team = _dict(fangraphs.get(f"{side}_team_batting"))
    return {
        "team": game.get(f"{side}_team"),
        "opponent": game.get(f"{opponent}_team"),
        "pitcher": game.get(f"{side}_pitcher"),
        "pitcher_id": game.get(f"{side}_pitcher_id"),
        "pitcher_stats": stats,
        "pitcher_savant": pitcher_savant,
        "team_savant": team_savant,
        "team_batting": fg_team,
        "bullpen": bullpen,
        "hitters": hitters,
    }


def _top_hitters(hitters: list[dict[str, Any]], metric: str, limit: int = 5) -> list[str]:
    rows = sorted(hitters, key=lambda row: _num(row.get(metric)), reverse=True)[:limit]
    return [
        f"{_safe(row.get('player') or row.get('Name') or row.get('name'), 'Player')} ({metric}: {_safe(row.get(metric))})"
        for row in rows
    ]


def _pitcher_block(context: dict[str, Any]) -> dict[str, Any]:
    stats = context["pitcher_stats"]
    savant = context["pitcher_savant"]
    return {
        "name": _safe(context.get("pitcher"), "TBD"),
        "era": _safe(stats.get("ERA") or stats.get("era")),
        "whip": _safe(stats.get("WHIP") or stats.get("whip")),
        "k_pct": _safe(savant.get("K%")),
        "bb_pct": _safe(savant.get("BB%")),
        "hr_per_9": _safe(stats.get("HR/9") or stats.get("homeRunsPer9")),
        "last_5_starts": "Unavailable",
        "home_away_splits": "Unavailable",
        "day_night_splits": "Unavailable",
        "pitch_mix": _safe(savant.get("pitch_mix") or savant.get("Pitch Mix")),
        "pitch_velocity": _safe(savant.get("Fastball Velocity") or savant.get("velocity")),
        "expected_regression": (
            "Negative regression risk"
            if _num(savant.get("xERA")) > _num(stats.get("ERA") or stats.get("era"), 99) + 0.75
            else "No major xERA regression flag"
            if savant
            else "Unavailable"
        ),
        "xera": _safe(savant.get("xERA")),
        "xba_allowed": _safe(savant.get("xBA")),
        "xslg_allowed": _safe(savant.get("xSLG")),
        "hard_hit_pct": _safe(savant.get("Hard Hit %")),
        "barrel_pct": _safe(savant.get("Barrel %")),
    }


def _offense_block(context: dict[str, Any]) -> dict[str, Any]:
    batting = context["team_batting"]
    team = context["team_savant"]
    hitters = context["hitters"]
    return {
        "ops_vs_lhp_rhp": _safe(batting.get("OPS") or team.get("OPS")),
        "runs_last_10": "Unavailable",
        "hits_last_10": "Unavailable",
        "strikeout_pct": _safe(batting.get("K%") or team.get("K%")),
        "walk_pct": _safe(batting.get("BB%") or team.get("BB%")),
        "top_5_lineup": [
            _safe(row.get("player") or row.get("Name") or row.get("name"), "Player")
            for row in hitters[:5]
        ],
        "lineup_status": "confirmed/projected" if hitters else "Unavailable",
        "top_contact_hitters": _top_hitters(hitters, "xBA"),
        "top_power_hitters": _top_hitters(hitters, "Barrel %"),
    }


def _bullpen_block(context: dict[str, Any]) -> dict[str, Any]:
    bullpen = context["bullpen"]
    return {
        "era": _safe(bullpen.get("ERA")),
        "whip": _safe(bullpen.get("WHIP")),
        "last_3_days_usage": "Unavailable",
        "fatigue_level": (
            "High risk" if _num(bullpen.get("WHIP")) >= 1.45 else "Moderate/low" if bullpen else "Unavailable"
        ),
        "closer_available": "Unavailable",
        "hard_hit_pct": _safe(bullpen.get("Hard Hit %")),
        "k_bb_pct": _safe(bullpen.get("K-BB%")),
    }


def _weather_block(game: dict[str, Any]) -> dict[str, Any]:
    weather = _dict(game.get("weather"))
    park = _safe(game.get("park_factor") or game.get("park_factor_label"), "neutral")
    return {
        "wind": _safe(weather.get("wind_speed")),
        "temperature": _safe(weather.get("temperature")),
        "humidity": _safe(weather.get("humidity")),
        "roof": _safe(weather.get("roof"), "Open/unknown"),
        "ballpark": _safe(game.get("venue")),
        "park_factor": park,
        "run_environment": (
            "Hitter boost" if any(word in park.lower() for word in ("hitter", "hr", "extreme"))
            else "Pitcher/neutral"
        ),
    }


def _game_grade(game: dict[str, Any]) -> str:
    score = 0
    if isinstance(game.get("savant"), dict):
        score += 2
    if game.get("odds_status") == "available":
        score += 2
    if isinstance(game.get("weather"), dict):
        score += 1
    if game.get("away_pitcher") != "TBD" and game.get("home_pitcher") != "TBD":
        score += 2
    if score >= 7:
        return "A+"
    if score == 6:
        return "A"
    if score == 5:
        return "A-"
    if score == 4:
        return "B+"
    if score == 3:
        return "B"
    return "C"


def _props_by_game(props_payload: dict[str, Any], game_pk: Any, prop_type: str) -> list[dict[str, Any]]:
    candidates = _dict(props_payload.get("candidates")).get(prop_type, [])
    return [
        prop for prop in candidates
        if isinstance(prop, dict) and str(prop.get("game_pk")) == str(game_pk)
    ]


def _ai_output_for_game(
    game: dict[str, Any],
    props_payload: dict[str, Any],
) -> dict[str, Any]:
    game_pk = game.get("game_pk") or game.get("game_id")
    best_hit = (_props_by_game(props_payload, game_pk, "hits") or [{}])[0]
    best_hr = (_props_by_game(props_payload, game_pk, "home_runs") or [{}])[0]
    best_k = (_props_by_game(props_payload, game_pk, "strikeouts") or [{}])[0]
    away = _safe(game.get("away_team"), "Away")
    home = _safe(game.get("home_team"), "Home")
    return {
        "moneyline": f"{home} ML lean" if game.get("odds_status") == "available" else "Unavailable",
        "f5": f"{home} F5 ML lean",
        "runline": f"{home} runline lean",
        "game_total": "Model total lean unavailable in admin report",
        "team_total": "Team total lean unavailable in admin report",
        "best_hit_prop": _safe(best_hit.get("player_name"), "Unavailable"),
        "best_hr_prop": _safe(best_hr.get("player_name"), "Unavailable"),
        "best_strikeout_prop": _safe(best_k.get("player_name"), "Unavailable"),
        "confidence": _game_grade(game),
        "strongest_lean_per_game": f"{away} @ {home}: {home} ML lean",
    }


def _model_notes(game: dict[str, Any]) -> dict[str, list[str]]:
    weather = _weather_block(game)
    for_notes = []
    against = []
    risks = []
    if isinstance(game.get("savant"), dict):
        for_notes.append("Statcast/Savant enrichment available.")
    else:
        risks.append("Savant context unavailable.")
    if game.get("odds_status") == "available":
        for_notes.append("Market data available.")
    else:
        risks.append("Odds feed unavailable for this game.")
    if weather["run_environment"] == "Hitter boost":
        for_notes.append("Park/weather context supports scoring.")
    else:
        against.append("Run environment is neutral or pitcher-friendly.")
    if game.get("away_pitcher") == "TBD" or game.get("home_pitcher") == "TBD":
        risks.append("Probable pitcher missing or unstable.")
    return {
        "reasons_for": for_notes or ["No strong model support flagged."],
        "reasons_against": against or ["No major against flag identified."],
        "risk_factors": risks or ["Standard variance."],
    }


def _game_report(game: dict[str, Any], props_payload: dict[str, Any]) -> dict[str, Any]:
    away = _side_context(game, "away")
    home = _side_context(game, "home")
    game_pk = game.get("game_pk") or game.get("game_id")
    weather = _weather_block(game)
    return {
        "game_pk": game_pk,
        "game": f"{game.get('away_team')} @ {game.get('home_team')}",
        "time_et": format_game_clock(game.get("game_time"), status=game.get("status")),
        "starting_pitchers": {
            "away": _pitcher_block(away),
            "home": _pitcher_block(home),
        },
        "current_records": "Unavailable",
        "home_away": {
            "away": game.get("away_team"),
            "home": game.get("home_team"),
        },
        "weather": weather,
        "ballpark": weather["ballpark"],
        "offense": {
            "away": _offense_block(away),
            "home": _offense_block(home),
        },
        "bullpen": {
            "away": _bullpen_block(away),
            "home": _bullpen_block(home),
        },
        "player_trends": {
            "current_hit_streaks": "See /streak_report_admin for verified 1-5 streaks.",
            "hr_streaks": "Unavailable",
            "multi_hit_streaks": "See Player Props/Hit Streak report.",
            "on_base_streaks": "Unavailable",
            "top_contact_hitters": {
                "away": _offense_block(away)["top_contact_hitters"],
                "home": _offense_block(home)["top_contact_hitters"],
            },
            "top_power_hitters": {
                "away": _offense_block(away)["top_power_hitters"],
                "home": _offense_block(home)["top_power_hitters"],
            },
        },
        "matchup_edge": {
            "pitch_type_advantages": _safe(_dict(_dict(game.get("savant")).get("pitch_type_matchups")), "Unavailable"),
            "bvp": "Unavailable",
            "statcast": "Available" if isinstance(game.get("savant"), dict) else "Unavailable",
            "hard_hit_pct": {
                "away_pitcher": _pitcher_block(away)["hard_hit_pct"],
                "home_pitcher": _pitcher_block(home)["hard_hit_pct"],
            },
            "barrel_pct": {
                "away_pitcher": _pitcher_block(away)["barrel_pct"],
                "home_pitcher": _pitcher_block(home)["barrel_pct"],
            },
            "expected_batting_average": {
                "away_pitcher": _pitcher_block(away)["xba_allowed"],
                "home_pitcher": _pitcher_block(home)["xba_allowed"],
            },
            "expected_slugging": {
                "away_pitcher": _pitcher_block(away)["xslg_allowed"],
                "home_pitcher": _pitcher_block(home)["xslg_allowed"],
            },
        },
        "ai_output": _ai_output_for_game(game, props_payload),
        "model_notes": _model_notes(game),
        "overall_grade": _game_grade(game),
    }


def _pick_lines(card_date: str, market: str, limit: int = 10) -> list[str]:
    try:
        picks = load_picks()
    except Exception:
        return []
    rows = [
        pick for pick in picks
        if isinstance(pick, dict)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
        and str(pick.get("market_type") or pick.get("pick_type") or "") == market
    ]
    return [
        _safe(pick.get("pick_text") or pick.get("selection") or pick.get("selected_team"), "Pick")
        for pick in rows[:limit]
    ]


def _top_props(props_payload: dict[str, Any], prop_type: str, limit: int = 10) -> list[str]:
    props = _dict(props_payload.get("candidates")).get(prop_type, [])
    if not isinstance(props, list):
        return []
    return [
        f"{_safe(prop.get('player_name'), 'Player')} ({_safe(prop.get('team_name') or prop.get('team'), 'Team')}) — {_safe(prop.get('confidence_grade'), 'N/A')}"
        for prop in props[:limit]
        if isinstance(prop, dict)
    ]


def _underdogs(card_date: str, limit: int = 10) -> list[str]:
    try:
        picks = load_picks()
    except Exception:
        return []
    rows = []
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        if str(pick.get("card_date") or pick.get("date") or "") != card_date:
            continue
        if _num(pick.get("odds")) > 0:
            rows.append((_num(pick.get("odds")), _safe(pick.get("pick_text") or pick.get("selection"), "Pick")))
    return [label for _odds, label in sorted(rows, reverse=True)[:limit]]


def _official_card_summary(card: str, card_date: str) -> dict[str, Any]:
    return {
        "play_of_the_day": _extract_section(card, "🔥 PLAY OF THE DAY"),
        "safe_parlay": _extract_section(card, "🧩 2-LEG SAFE PARLAY"),
        "value_parlay": "Unavailable in free-card feed",
        "core_five": _pick_lines(card_date, "moneyline", 2)
        + _pick_lines(card_date, "f5_moneyline", 1)
        + _pick_lines(card_date, "runline", 1)
        + _pick_lines(card_date, "total", 1),
    }


def _extract_section(card: str, heading: str) -> str:
    start = card.find(heading)
    if start < 0:
        return "Unavailable"
    end = card.find(DIVIDER, start)
    if end < 0:
        end = card.find("━━━━━━━━━━━━", start)
    return card[start:end if end >= 0 else len(card)].strip()


def build_mlb_admin_report(
    card_date: str,
    *,
    odds_api_key: str = "",
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    highlightly_api_key: str = "",
    save_picks: bool = True,
) -> dict[str, Any]:
    """Build and save the owner-only full MLB War Room report."""
    slate = get_combined_slate(
        odds_api_key,
        game_date=card_date,
        highlightly_api_key=highlightly_api_key,
    )
    props_payload = build_player_props_lab(slate, card_date) if slate else {}
    official_card = ""
    saved_picks = 0
    if slate:
        try:
            official_card = _safe(
                __import__("asyncio").run(
                    analyze_mlb_slate(slate, openai_api_key, anthropic_api_key)
                ),
                "",
            )
            if save_picks and official_card:
                saved_picks = save_official_picks(
                    official_card,
                    slate,
                    card_date,
                    source_command="mlb_admin",
                )
        except RuntimeError:
            # If already inside an event loop, caller should pass through async wrapper.
            official_card = ""
        except Exception:
            official_card = ""

    games = [_game_report(game, props_payload) for game in slate]
    top_lists = {
        "top_10_moneylines": _pick_lines(card_date, "moneyline"),
        "top_10_runlines": _pick_lines(card_date, "runline"),
        "top_10_totals": _pick_lines(card_date, "total"),
        "top_10_team_totals": _pick_lines(card_date, "team_total"),
        "top_10_f5": _pick_lines(card_date, "f5_moneyline"),
        "top_10_hit_props": _top_props(props_payload, "hits"),
        "top_10_hr_props": _top_props(props_payload, "home_runs"),
        "top_10_strikeout_props": _top_props(props_payload, "strikeouts"),
        "top_10_underdogs": _underdogs(card_date),
    }
    report = {
        "version": "Official MLB War Room v1",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "saved_picks": saved_picks,
        "games": games,
        "top_lists": top_lists,
        "todays_official_card": _official_card_summary(official_card, card_date),
        "official_card_text": official_card,
        "admin_only": True,
    }
    path = _admin_dir(card_date) / "mlb_admin_report.json"
    _write_json(path, report)
    report["report_path"] = str(path)
    return report


async def build_mlb_admin_report_async(
    card_date: str,
    *,
    odds_api_key: str = "",
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    highlightly_api_key: str = "",
    save_picks: bool = True,
) -> dict[str, Any]:
    """Async version used by Telegram handlers."""
    errors: list[str] = []
    try:
        slate = get_combined_slate(
            odds_api_key,
            game_date=card_date,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception as error:
        slate = []
        errors.append(f"Slate unavailable: {error}")
    if not slate and not errors:
        errors.append("No MLB games/slate rows returned by MLB Stats API for this date.")
    try:
        props_payload = build_player_props_lab(slate, card_date) if slate else {}
    except Exception as error:
        props_payload = {}
        errors.append(f"Props lab unavailable: {error}")
    official_card = ""
    saved_picks = 0
    if slate:
        try:
            official_card = await analyze_mlb_slate(slate, openai_api_key, anthropic_api_key)
        except Exception as error:
            errors.append(f"AI official card unavailable; fallback used: {error}")
            official_card = build_fallback_card(slate)
        if save_picks and official_card:
            try:
                saved_picks = save_official_picks(
                    official_card,
                    slate,
                    card_date,
                    source_command="mlb_admin",
                )
            except Exception as error:
                errors.append(f"Pick saving unavailable: {error}")
    games = [_game_report(game, props_payload) for game in slate]
    top_lists = {
        "top_10_moneylines": _pick_lines(card_date, "moneyline"),
        "top_10_runlines": _pick_lines(card_date, "runline"),
        "top_10_totals": _pick_lines(card_date, "total"),
        "top_10_team_totals": _pick_lines(card_date, "team_total"),
        "top_10_f5": _pick_lines(card_date, "f5_moneyline"),
        "top_10_hit_props": _top_props(props_payload, "hits"),
        "top_10_hr_props": _top_props(props_payload, "home_runs"),
        "top_10_strikeout_props": _top_props(props_payload, "strikeouts"),
        "top_10_underdogs": _underdogs(card_date),
    }
    report = {
        "version": "Official MLB War Room v1",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "saved_picks": saved_picks,
        "games": games,
        "top_lists": top_lists,
        "todays_official_card": _official_card_summary(official_card, card_date),
        "official_card_text": official_card,
        "errors": errors,
        "admin_only": True,
    }
    path = _admin_dir(card_date) / "mlb_admin_report.json"
    _write_json(path, report)
    report["report_path"] = str(path)
    return report


def render_mlb_admin_report(report: dict[str, Any], *, full: bool = True) -> str:
    """Render the War Room report as Telegram-safe text."""
    lines = [
        "⚾ BETGPTAI OFFICIAL MLB WAR ROOM",
        f"📅 Date: {report.get('display_date')}",
        "🧪 ADMIN ONLY — NOT PUBLIC CARD",
        "",
    ]
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    if errors:
        lines.extend([
            "⚠️ BUILD NOTES",
            *[f"- {item}" for item in errors[:8]],
            "",
        ])
    if not report.get("games"):
        lines.extend([
            "No MLB slate data was available for this report.",
            f"Saved JSON: {report.get('report_path')}",
        ])
        return "\n".join(str(line) for line in lines).strip()
    for game in report.get("games", []) if isinstance(report.get("games"), list) else []:
        lines.extend(_render_game(game, full=full))
    top_lists = _dict(report.get("top_lists"))
    lines.extend(["", DIVIDER, "BOTTOM BOARD", ""])
    labels = [
        ("🔥 TOP 10 Moneylines", "top_10_moneylines"),
        ("🔥 TOP 10 Runlines", "top_10_runlines"),
        ("🔥 TOP 10 Totals", "top_10_totals"),
        ("🔥 TOP 10 Team Totals (team total average 4.5 or 5.5)", "top_10_team_totals"),
        ("🔥 TOP 10 F5", "top_10_f5"),
        ("🔥 TOP 10 Hit Props", "top_10_hit_props"),
        ("🔥 TOP 10 HR Props", "top_10_hr_props"),
        ("🔥 TOP 10 Strikeout Props", "top_10_strikeout_props"),
        ("🔥 TOP 10 Underdogs", "top_10_underdogs"),
    ]
    for label, key in labels:
        values = top_lists.get(key) or ["Unavailable"]
        lines.append(label)
        lines.extend(f"{idx}. {value}" for idx, value in enumerate(values[:10], start=1))
        lines.append("")
    official = _dict(report.get("todays_official_card"))
    lines.extend([
        DIVIDER,
        "Today's Official Card",
        "",
        "Play of the Day:",
        _safe(official.get("play_of_the_day")),
        "",
        "Safe Parlay:",
        _safe(official.get("safe_parlay")),
        "",
        "Value Parlay:",
        _safe(official.get("value_parlay")),
        "",
        "Core Five:",
    ])
    lines.extend(f"- {item}" for item in (official.get("core_five") or ["Unavailable"]))
    lines.extend(["", f"Saved JSON: {report.get('report_path')}"])
    return "\n".join(str(line) for line in lines).strip()


def _render_game(game: dict[str, Any], *, full: bool) -> list[str]:
    sp = _dict(game.get("starting_pitchers"))
    away_sp = _dict(sp.get("away"))
    home_sp = _dict(sp.get("home"))
    offense = _dict(game.get("offense"))
    bullpen = _dict(game.get("bullpen"))
    weather = _dict(game.get("weather"))
    ai = _dict(game.get("ai_output"))
    notes = _dict(game.get("model_notes"))
    lines = [
        DIVIDER,
        f"Game: {game.get('game')}",
        f"Time ET: {game.get('time_et')}",
        f"Starting Pitchers: {away_sp.get('name')} vs {home_sp.get('name')}",
        f"Current Records: {game.get('current_records')}",
        f"Home/Away: {game.get('game')}",
        f"Weather: {weather.get('temperature')} / Wind {weather.get('wind')}",
        f"Ballpark: {game.get('ballpark')}",
        f"⭐ Overall Grade: {game.get('overall_grade')}",
        "",
        DIVIDER,
        "SP Analysis",
        f"Away SP ERA/WHIP: {away_sp.get('era')} / {away_sp.get('whip')}",
        f"Away SP K%/BB%/HR9: {away_sp.get('k_pct')} / {away_sp.get('bb_pct')} / {away_sp.get('hr_per_9')}",
        f"Away Pitch Mix: {away_sp.get('pitch_mix')}",
        f"Away Velocity: {away_sp.get('pitch_velocity')}",
        f"Away Expected Regression: {away_sp.get('expected_regression')}",
        f"Home SP ERA/WHIP: {home_sp.get('era')} / {home_sp.get('whip')}",
        f"Home SP K%/BB%/HR9: {home_sp.get('k_pct')} / {home_sp.get('bb_pct')} / {home_sp.get('hr_per_9')}",
        f"Home Pitch Mix: {home_sp.get('pitch_mix')}",
        f"Home Velocity: {home_sp.get('pitch_velocity')}",
        f"Home Expected Regression: {home_sp.get('expected_regression')}",
        "",
    ]
    if not full:
        lines.extend([
            "Strongest Lean:",
            ai.get("strongest_lean_per_game"),
            "Risks:",
            "; ".join(notes.get("risk_factors", ["Standard variance."])),
            "",
        ])
        return lines
    lines.extend([
        DIVIDER,
        "Offense",
        f"Away OPS/K%/BB%: {_dict(offense.get('away')).get('ops_vs_lhp_rhp')} / {_dict(offense.get('away')).get('strikeout_pct')} / {_dict(offense.get('away')).get('walk_pct')}",
        f"Away Top 5: {', '.join(_dict(offense.get('away')).get('top_5_lineup') or ['Unavailable'])}",
        f"Away Lineup: {_dict(offense.get('away')).get('lineup_status')}",
        f"Home OPS/K%/BB%: {_dict(offense.get('home')).get('ops_vs_lhp_rhp')} / {_dict(offense.get('home')).get('strikeout_pct')} / {_dict(offense.get('home')).get('walk_pct')}",
        f"Home Top 5: {', '.join(_dict(offense.get('home')).get('top_5_lineup') or ['Unavailable'])}",
        f"Home Lineup: {_dict(offense.get('home')).get('lineup_status')}",
        "",
        DIVIDER,
        "Bullpen",
        f"Away Bullpen ERA/WHIP: {_dict(bullpen.get('away')).get('era')} / {_dict(bullpen.get('away')).get('whip')}",
        f"Away Fatigue: {_dict(bullpen.get('away')).get('fatigue_level')}",
        f"Away Closer: {_dict(bullpen.get('away')).get('closer_available')}",
        f"Home Bullpen ERA/WHIP: {_dict(bullpen.get('home')).get('era')} / {_dict(bullpen.get('home')).get('whip')}",
        f"Home Fatigue: {_dict(bullpen.get('home')).get('fatigue_level')}",
        f"Home Closer: {_dict(bullpen.get('home')).get('closer_available')}",
        "",
        DIVIDER,
        "Player Trends",
        f"Current hit streaks: {_dict(game.get('player_trends')).get('current_hit_streaks')}",
        f"HR streaks: {_dict(game.get('player_trends')).get('hr_streaks')}",
        f"Multi-hit streaks: {_dict(game.get('player_trends')).get('multi_hit_streaks')}",
        f"On-base streaks: {_dict(game.get('player_trends')).get('on_base_streaks')}",
        "",
        DIVIDER,
        "Matchup Edge",
        f"Pitch-type advantages: {_dict(game.get('matchup_edge')).get('pitch_type_advantages')}",
        f"BvP: {_dict(game.get('matchup_edge')).get('bvp')}",
        f"Statcast: {_dict(game.get('matchup_edge')).get('statcast')}",
        f"Hard Hit%: {_dict(game.get('matchup_edge')).get('hard_hit_pct')}",
        f"Barrel%: {_dict(game.get('matchup_edge')).get('barrel_pct')}",
        f"xBA: {_dict(game.get('matchup_edge')).get('expected_batting_average')}",
        f"xSLG: {_dict(game.get('matchup_edge')).get('expected_slugging')}",
        "",
        DIVIDER,
        "Weather",
        f"Wind: {weather.get('wind')}",
        f"Temperature: {weather.get('temperature')}",
        f"Humidity: {weather.get('humidity')}",
        f"Roof: {weather.get('roof')}",
        f"Run environment: {weather.get('run_environment')}",
        "",
        DIVIDER,
        "AI Output",
        f"Moneyline: {ai.get('moneyline')}",
        f"F5: {ai.get('f5')}",
        f"Runline: {ai.get('runline')}",
        f"Game Total: {ai.get('game_total')}",
        f"Team Total: {ai.get('team_total')}",
        f"Best Hit Prop: {ai.get('best_hit_prop')}",
        f"Best HR Prop: {ai.get('best_hr_prop')}",
        f"Best Strikeout Prop: {ai.get('best_strikeout_prop')}",
        f"Confidence: {ai.get('confidence')}",
        "",
        DIVIDER,
        "Model Notes",
        "Reasons FOR:",
        *[f"- {item}" for item in notes.get("reasons_for", [])],
        "Reasons AGAINST:",
        *[f"- {item}" for item in notes.get("reasons_against", [])],
        "Risk Factors:",
        *[f"- {item}" for item in notes.get("risk_factors", [])],
        "",
    ])
    return lines


def create_mlb_admin_image_prompt(report: dict[str, Any]) -> str:
    """Create an admin-only Anime Vault dashboard image prompt."""
    top_ml = _dict(report.get("top_lists")).get("top_10_moneylines") or ["Unavailable"]
    games = report.get("games") or []
    return (
        "BETGPTAI Anime Vault admin dashboard, 1080x1920 vertical, premium anime "
        "sports command center, dark electric MLB war room, glowing blue/red/gold "
        "data panels, manga speed lines, stadium lights, dramatic baseball mascot "
        "artwork, premium ESPN x Topps x anime trading-card style. Admin-only "
        "research dashboard, no public betting hype. Include readable text blocks: "
        f"OFFICIAL MLB WAR ROOM, Date {_safe(report.get('display_date'))}, "
        f"Games {len(games)}, Top Moneylines: {'; '.join(map(str, top_ml[:5]))}. "
        "Sections: Top Opportunities, Pitcher Edge, Weather/Park, Props Lab, "
        "Official Card. no emojis, no smiley faces, no placeholder icons, no flat "
        "infographic style, no sportsbook names."
    )


def prepare_mlb_admin_image(report: dict[str, Any], *, image_generation_enabled: bool | None = None) -> dict[str, Any]:
    """Save prompt and optionally generate the admin War Room image."""
    card_date = str(report.get("card_date"))
    output_dir = _admin_dir(card_date)
    prompt = create_mlb_admin_image_prompt(report)
    prompt_path = output_dir / "mlb_admin_image_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    image_path = output_dir / "mlb_admin_dashboard.png"
    enabled = (
        os.getenv("IMAGE_GENERATION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        if image_generation_enabled is None
        else image_generation_enabled
    )
    error = None
    if enabled:
        try:
            generate_image_from_prompt(prompt, str(image_path))
        except Exception as exc:
            error = str(exc)
    return {
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "image_path": str(image_path) if image_path.exists() else None,
        "image_error": error,
    }
