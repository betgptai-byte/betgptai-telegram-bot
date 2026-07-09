"""BETGPTAI Intelligence Dashboard v1.

Admin-only research support. This module reads and summarizes existing BETGPTAI
data sources without posting to members or changing public picks.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hitting_streak_report import (
    build_hitting_streak_report,
    render_hitting_streak_report,
)
from mlb_admin_report import build_mlb_top5_admin_card
from mlb_data import get_combined_slate, get_mlb_schedule
from model_report import load_model_report
from player_props_engine import build_player_props_lab
from results_tracker import load_picks
from storage import data_file, storage_status
from verification_engine import average_verification_score, enrich_mlb_slate_verification


EASTERN = ZoneInfo("America/New_York")
UNAVAILABLE = "unavailable"


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", ""))
    except (TypeError, ValueError):
        return default


def _safe(value: Any, fallback: str = "Unavailable") -> str:
    text = str(value or "").strip()
    return text if text and text.lower() != UNAVAILABLE else fallback


def _report_dir(card_date: str) -> Path:
    path = data_file("reports") / card_date
    path.mkdir(parents=True, exist_ok=True)
    return path


def _model_review_dir() -> Path:
    path = data_file("model_reviews")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _source_statuses(card_date: str, slate: list[dict[str, Any]]) -> dict[str, str]:
    report = load_model_report(card_date) or {}
    sources = report.get("sources") if isinstance(report.get("sources"), dict) else {}
    return {
        "MLB Stats API": "✅ Available" if slate else "❌ Unavailable",
        "Baseball Savant": "✅ Used" if sources.get("baseball_savant") or any(isinstance(g.get("savant"), dict) for g in slate) else "➖ Optional unavailable",
        "Weather": "✅ Available" if any(g.get("weather") not in (None, "", UNAVAILABLE, {}) for g in slate) else "➖ Optional unavailable",
        "Sharp API": "✅ Primary" if os.getenv("SHARP_API_KEY", "").strip() else "➖ Not configured",
        "Odds API": "✅ Available" if any(g.get("odds_status") == "available" for g in slate) else "➖ Optional unavailable",
        "OpenAI": "✅ Configured" if os.getenv("OPENAI_API_KEY", "").strip() else "❌ Missing",
        "Claude": "✅ Configured" if os.getenv("ANTHROPIC_API_KEY", "").strip() else "❌ Missing",
    }


def _image_status(card_date: str) -> str:
    base = data_file("generated_cards")
    iso_dir = base / card_date
    mmddyyyy = datetime.fromisoformat(card_date).strftime("%m-%d-%Y")
    prop_dir = base / mmddyyyy
    candidates = [
        iso_dir / "mlb_auto_card.png",
        iso_dir / "today_pick.png",
        prop_dir / "best_hit_prop.png",
        *[iso_dir / f"slide_{index}.png" for index in range(1, 8)],
    ]
    prompt_candidates = [
        iso_dir / "mlb_auto_prompt.txt",
        iso_dir / "today_pick_prompt.txt",
        prop_dir / "best_hit_art_prompt.txt",
        *[iso_dir / f"slide_{index}_prompt.txt" for index in range(1, 8)],
    ]
    images = sum(path.exists() for path in candidates)
    prompts = sum(path.exists() for path in prompt_candidates)
    if images:
        return f"✅ {images} image(s) ready"
    if prompts:
        return f"✅ Prompt fallback ready ({prompts})"
    return "➖ No images/prompts yet"


def _starting_pitcher_status(slate: list[dict[str, Any]]) -> str:
    total = len(slate) * 2
    if not total:
        return "No games"
    known = sum(
        1
        for game in slate
        for key in ("away_pitcher", "home_pitcher")
        if _safe(game.get(key), "TBD") != "TBD"
    )
    return f"{known}/{total} probable starters listed"


def _lineup_status(props_payload: dict[str, Any]) -> str:
    source = props_payload.get("source_status") if isinstance(props_payload, dict) else {}
    if isinstance(source, dict) and source.get("lineups"):
        return "✅ Confirmed/projected lineup context available"
    return "➖ Lineup context limited"


def _top_props(props_payload: dict[str, Any], prop_type: str, limit: int = 10) -> list[dict[str, Any]]:
    candidates = props_payload.get("candidates") if isinstance(props_payload, dict) else {}
    rows = candidates.get(prop_type) if isinstance(candidates, dict) else []
    return [row for row in rows if isinstance(row, dict)][:limit] if isinstance(rows, list) else []


def _prop_line(prop: dict[str, Any]) -> str:
    player = _safe(prop.get("player_name"), "Player")
    team = _safe(prop.get("team_name") or prop.get("team"), "Team")
    grade = _safe(prop.get("confidence_grade"), "N/A")
    market = str(prop.get("prop_type") or "prop").replace("_", " ").title()
    line = prop.get("line")
    return f"{player} ({team}) — {market} {line if line is not None else ''} — {grade}".strip()


def _lineup_label(prop: dict[str, Any]) -> str:
    lineup = prop.get("lineup_verification") if isinstance(prop.get("lineup_verification"), dict) else {}
    return str(lineup.get("status") or lineup.get("state") or "projected").lower()


def _hit_prop_line(prop: dict[str, Any]) -> str:
    player = _safe(prop.get("player_name"), "Player")
    team = _safe(prop.get("team_name") or prop.get("team"), "Team")
    opponent = _safe(prop.get("opponent_name") or prop.get("opponent"), "Opponent")
    return f"{player} — {team} — {opponent} — Over 0.5 Hits — Lineup: {_lineup_label(prop)}"


def _hr_watch_line(prop: dict[str, Any]) -> str:
    player = _safe(prop.get("player_name"), "Player")
    team = _safe(prop.get("team_name") or prop.get("team"), "Team")
    opponent = _safe(prop.get("opponent_name") or prop.get("opponent"), "Opponent")
    return f"{player} — {team} — {opponent}"


def _strikeout_prop_line(prop: dict[str, Any]) -> str:
    player = _safe(prop.get("player_name"), "Pitcher")
    team = _safe(prop.get("team_name") or prop.get("team"), "Team")
    opponent = _safe(prop.get("opponent_name") or prop.get("opponent"), "Opponent")
    line = prop.get("line")
    line_text = f"Over {line} Ks" if line not in (None, "", "unavailable") else "Over Ks"
    return f"{player} — {team} — {opponent} — {line_text}"


def _prop_empty_reason(props_payload: dict[str, Any], prop_family: str) -> str:
    """Give an exact admin reason when prop candidates are empty."""
    debug = props_payload.get("debug") if isinstance(props_payload, dict) else {}
    if not props_payload:
        return "No props available because the MLB props engine did not return a payload."
    if int(debug.get("players_scanned") or 0) == 0 and prop_family in {"hits", "home_runs"}:
        return "No hit props available because projected lineups were unavailable."
    if int(debug.get("starting_pitchers_scanned") or 0) == 0 and prop_family == "strikeouts":
        return "No strikeout props available because probable pitchers were missing."
    rejected = debug.get("rejected_props") if isinstance(debug.get("rejected_props"), list) else []
    verification = debug.get("player_verification_issues") if isinstance(debug.get("player_verification_issues"), list) else []
    reason_counts = debug.get("reason_counts") if isinstance(debug.get("reason_counts"), dict) else {}
    if verification:
        if reason_counts:
            top_reasons = ", ".join(
                f"{reason} ({count})"
                for reason, count in sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:3]
            )
            return f"No props available because all candidates failed verification/filtering: {top_reasons}."
        return "No props available because all candidates failed player verification/filtering."
    if rejected:
        return f"No props available because candidates were rejected: {rejected[0]}"
    missing = debug.get("missing_fields") if isinstance(debug.get("missing_fields"), list) else []
    if missing:
        return f"No props available because required context was limited: {', '.join(missing)}."
    return "No props available because no candidate reached the admin confidence threshold."


def _admin_pick_line(candidate: dict[str, Any], fallback_market: str) -> str:
    pick = _safe(candidate.get("pick_text") or candidate.get("selection"), "Pick")
    if candidate.get("inferred_line_admin_only"):
        pick = f"{pick} (Inferred line — admin only)"
    elif fallback_market == "team_total":
        line = candidate.get("line")
        if line not in (None, "", "unavailable") and str(line) not in pick:
            pick = f"{pick} {line}"
    edge = candidate.get("final_edge_score")
    if edge is not None:
        pick = f"{pick} — Edge {edge}"
    return pick


def _mlb_top5_lists(card_date: str, odds_api_key: str, highlightly_api_key: str) -> dict[str, Any]:
    """Build MLB-only top opportunity lists from the MLB admin Top 5 engine."""
    try:
        report = build_mlb_top5_admin_card(
            card_date,
            odds_api_key=odds_api_key,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception:
        return {
            "team_totals": [],
            "game_totals": [],
            "moneylines": [],
            "f5_moneylines": [],
            "market_debug": {},
        }
    top5 = report.get("top5") if isinstance(report.get("top5"), dict) else {}
    return {
        "team_totals": [
            _admin_pick_line(item, "team_total")
            for item in (top5.get("team_totals") or [])
            if isinstance(item, dict)
        ],
        "game_totals": [
            _admin_pick_line(item, "total")
            for item in (top5.get("game_totals") or [])
            if isinstance(item, dict)
        ],
        "moneylines": [
            _admin_pick_line(item, "moneyline")
            for item in (top5.get("moneyline") or [])
            if isinstance(item, dict)
        ],
        "f5_moneylines": [
            _admin_pick_line(item, "f5_moneyline")
            for item in (top5.get("f5_moneyline") or [])
            if isinstance(item, dict)
        ],
        "market_debug": report.get("market_debug") if isinstance(report.get("market_debug"), dict) else {},
    }


def _extract_saved_picks(card_date: str, market_type: str, limit: int = 10) -> list[str]:
    try:
        picks = load_picks()
    except Exception:
        return []
    def is_mlb_pick(pick: dict[str, Any]) -> bool:
        sport = str(pick.get("sport") or "").lower()
        return sport == "mlb" or (not sport and bool(pick.get("game_pk")))

    rows = [
        pick for pick in picks
        if isinstance(pick, dict)
        and is_mlb_pick(pick)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
        and str(pick.get("market_type") or pick.get("pick_type") or "") == market_type
    ]
    return [
        _safe(pick.get("pick_text") or pick.get("selection") or pick.get("selected_team"), "Pick")
        for pick in rows[:limit]
    ]


def _top_underdogs(card_date: str, limit: int = 10) -> list[str]:
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
        sport = str(pick.get("sport") or "").lower()
        if sport == "soccer" or (not sport and not pick.get("game_pk")):
            continue
        odds = _num(pick.get("odds"), 0)
        if odds > 0:
            rows.append((odds, _safe(pick.get("pick_text") or pick.get("selection"), "Pick")))
    return [label for _odds, label in sorted(rows, reverse=True)[:limit]]


def _soccer_candidates_count(card_date: str) -> int:
    try:
        picks = load_picks()
    except Exception:
        return 0
    return sum(
        1 for pick in picks
        if isinstance(pick, dict)
        and str(pick.get("sport") or "").lower() == "soccer"
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
    )


def _pitcher_reports(slate: list[dict[str, Any]]) -> dict[str, list[str]]:
    rows: list[dict[str, Any]] = []
    for game in slate:
        savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
        for side in ("away", "home"):
            pitcher = _safe(game.get(f"{side}_pitcher"), "TBD")
            if pitcher == "TBD":
                continue
            stats = game.get(f"{side}_pitcher_stats") if isinstance(game.get(f"{side}_pitcher_stats"), dict) else {}
            s_metrics = savant.get(f"{side}_pitcher") if isinstance(savant.get(f"{side}_pitcher"), dict) else {}
            rows.append(
                {
                    "pitcher": pitcher,
                    "team": game.get(f"{side}_team"),
                    "hits": _num(stats.get("H"), 0),
                    "whip": _num(stats.get("WHIP") or stats.get("whip"), 0),
                    "xba": _num(s_metrics.get("xBA"), 0),
                    "hardhit": _num(s_metrics.get("Hard Hit %"), 0),
                    "whiff": _num(s_metrics.get("Whiff %"), 0),
                    "xera": _num(s_metrics.get("xERA"), 0),
                }
            )

    def top(key: str, reverse: bool = True) -> list[str]:
        selected = sorted(rows, key=lambda row: row.get(key, 0), reverse=reverse)[:10]
        return [f"{row['pitcher']} ({row.get('team')}) — {key}: {row.get(key)}" for row in selected]

    return {
        "most_hits_allowed": top("hits"),
        "highest_whip": top("whip"),
        "highest_xba_allowed": top("xba"),
        "highest_hardhit_allowed": top("hardhit"),
        "lowest_whiff_proxy": top("whiff", reverse=False),
        "regression_candidates": top("xera"),
    }


def _bullpen_reports(slate: list[dict[str, Any]]) -> dict[str, list[str]]:
    rows = []
    for game in slate:
        savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
        for side in ("away", "home"):
            team = game.get(f"{side}_team")
            bullpen = savant.get(f"{side}_bullpen") if isinstance(savant.get(f"{side}_bullpen"), dict) else {}
            rows.append(
                {
                    "team": team,
                    "whip": _num(bullpen.get("WHIP"), 0),
                    "hardhit": _num(bullpen.get("Hard Hit %"), 0),
                    "kbb": _num(bullpen.get("K-BB%"), 0),
                    "xfip": _num(bullpen.get("xFIP"), 0),
                }
            )
    worst = sorted(rows, key=lambda row: (row["whip"], row["hardhit"]), reverse=True)[:10]
    best = sorted(rows, key=lambda row: (row["whip"] or 99, -row["kbb"]))[:10]
    return {
        "worst_bullpens": [f"{row['team']} — WHIP {row['whip']} / HardHit {row['hardhit']}" for row in worst],
        "best_bullpens": [f"{row['team']} — WHIP {row['whip']} / K-BB {row['kbb']}" for row in best],
        "fatigue": ["Bullpen recent-usage/fatigue requires confirmed recent usage feed."],
    }


def _pitch_type_report(slate: list[dict[str, Any]]) -> list[str]:
    lines = []
    for game in slate:
        savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
        matchups = savant.get("pitch_type_matchups") if isinstance(savant.get("pitch_type_matchups"), dict) else {}
        if matchups:
            lines.append(
                f"{game.get('away_team')} @ {game.get('home_team')}: "
                f"{len(matchups)} arsenal matchup section(s) available"
            )
    return lines[:10] or ["Pitch-type matchup data unavailable from current Savant enrichment."]


def _weather_park_edges(slate: list[dict[str, Any]]) -> list[str]:
    lines = []
    for game in slate:
        weather = game.get("weather") if isinstance(game.get("weather"), dict) else {}
        park = _safe(game.get("park_factor") or game.get("park_factor_label"), "neutral")
        temp = _safe(weather.get("temperature"), "temp n/a")
        wind = _safe(weather.get("wind_speed"), "wind n/a")
        lines.append(
            f"{game.get('away_team')} @ {game.get('home_team')} — Park: {park}; Temp: {temp}; Wind: {wind}"
        )
    return lines[:15]


def _trend_sections(streak_payload: dict[str, Any]) -> dict[str, list[str]]:
    games = streak_payload.get("games") if isinstance(streak_payload.get("games"), dict) else {}
    hit_streaks: list[str] = []
    multi_hit: list[str] = []
    for game_label, players in games.items():
        if not isinstance(players, list):
            continue
        for player in players:
            if not isinstance(player, dict):
                continue
            name = _safe(player.get("player_name"), "Player")
            hit_streaks.append(
                f"{name} — {player.get('current_hit_streak')} — {game_label}"
            )
            if _num(player.get("multi_hit_games_last_10"), 0) >= 3:
                multi_hit.append(
                    f"{name} — {player.get('multi_hit_games_last_10')} multi-hit games last 10"
                )
    return {
        "lineup_hit_streaks": hit_streaks[:20],
        "multi_hit_trends": multi_hit[:20] or ["No major multi-hit trend flagged."],
        "on_base_streaks": ["On-base streak feed not yet connected."],
        "hr_streaks": ["HR streak feed not yet connected."],
        "rbi_streaks": ["RBI streak feed not yet connected."],
    }


def _daily_review(card_date: str) -> dict[str, Any]:
    picks = [
        pick for pick in load_picks()
        if isinstance(pick, dict)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
    ]
    graded = [pick for pick in picks if pick.get("result") in {"win", "loss", "push"}]
    losses = [pick for pick in graded if pick.get("result") == "loss"]
    tagged_losses = []
    for pick in losses:
        text = str(pick.get("pick_text") or pick.get("selection") or "").lower()
        reason = "variance"
        if "bullpen" in text:
            reason = "bullpen"
        elif "under" in text or "over" in text:
            reason = "late scoring"
        elif "line" in text:
            reason = "bad market"
        tagged_losses.append(
            {
                "pick": pick.get("pick_text") or pick.get("selection"),
                "tag": reason,
            }
        )
    review = {
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "graded_picks": len(graded),
        "wins": sum(1 for pick in graded if pick.get("result") == "win"),
        "losses": len(losses),
        "pushes": sum(1 for pick in graded if pick.get("result") == "push"),
        "loss_reviews": tagged_losses,
        "available": bool(graded),
    }
    _write_json(_model_review_dir() / f"{card_date}.json", review)
    return review


def build_intelligence_dashboard(
    card_date: str,
    *,
    odds_api_key: str = "",
    highlightly_api_key: str = "",
) -> dict[str, Any]:
    """Build and save the full admin-only Intelligence Dashboard payload."""
    errors: list[str] = []
    try:
        slate = get_combined_slate(
            odds_api_key,
            game_date=card_date,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception as error:
        errors.append(f"Combined MLB slate unavailable: {error}")
        try:
            slate = get_mlb_schedule(card_date)
        except Exception as schedule_error:
            errors.append(f"MLB schedule unavailable: {schedule_error}")
            slate = []
    try:
        slate = enrich_mlb_slate_verification(slate, card_date) if slate else slate
    except Exception as error:
        errors.append(f"ESPN verification unavailable: {error}")

    try:
        props = build_player_props_lab(slate, card_date) if slate else {}
    except Exception as error:
        errors.append(f"Props lab unavailable: {error}")
        props = {}

    try:
        streak_report = build_hitting_streak_report(
            card_date,
            odds_api_key=odds_api_key,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception as error:
        errors.append(f"Hitting streak report unavailable: {error}")
        streak_report = {"games": {}, "debug": {}}

    storage = storage_status()
    statuses = _source_statuses(card_date, slate)
    pitcher_reports = _pitcher_reports(slate)
    bullpen_reports = _bullpen_reports(slate)
    trends = _trend_sections(streak_report)
    mlb_top5 = _mlb_top5_lists(card_date, odds_api_key, highlightly_api_key)
    props_debug = props.get("debug") if isinstance(props.get("debug"), dict) else {}
    streak_debug = streak_report.get("debug") if isinstance(streak_report.get("debug"), dict) else {}
    soccer_count = _soccer_candidates_count(card_date)
    hit_props = _top_props(props, "hits")
    hr_watch = _top_props(props, "home_runs")
    strikeouts = _top_props(props, "strikeouts")
    matched_odds_games = sum(1 for game in slate if game.get("odds_status") == "available")
    market_empty_reason = (
        "No qualified MLB candidates available from current engines."
    )

    dashboard = {
        "version": "BETGPTAI Intelligence Dashboard v2",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "daily_health": {
            "storage_status": "healthy" if storage.get("results_database_healthy") else "unhealthy",
            "apis_status": statuses,
            "today_mlb_games": len(slate),
            "verification_score": average_verification_score(slate),
            "lineup_status": _lineup_status(props),
            "starting_pitcher_status": _starting_pitcher_status(slate),
            "images_status": _image_status(card_date),
            "results_storage_status": "healthy" if storage.get("results_database_healthy") else "unhealthy",
        },
        "mlb_top_opportunities": {
            "top_hit_props": [_hit_prop_line(prop) for prop in hit_props],
            "top_hit_props_reason": "" if hit_props else _prop_empty_reason(props, "hits"),
            "hr_watch": [_hr_watch_line(prop) for prop in hr_watch],
            "hr_watch_reason": "" if hr_watch else _prop_empty_reason(props, "home_runs"),
            "strikeout_props": [_strikeout_prop_line(prop) for prop in strikeouts],
            "strikeout_props_reason": "" if strikeouts else _prop_empty_reason(props, "strikeouts"),
            "team_totals": mlb_top5.get("team_totals") or [],
            "team_totals_reason": "" if mlb_top5.get("team_totals") else market_empty_reason,
            "game_totals": mlb_top5.get("game_totals") or [],
            "game_totals_reason": "" if mlb_top5.get("game_totals") else market_empty_reason,
            "moneylines": mlb_top5.get("moneylines") or [],
            "moneylines_reason": "" if mlb_top5.get("moneylines") else market_empty_reason,
            "f5_moneylines": mlb_top5.get("f5_moneylines") or [],
            "f5_moneylines_reason": "" if mlb_top5.get("f5_moneylines") else market_empty_reason,
            "top_underdogs": _top_underdogs(card_date),
        },
        "top_opportunities": {
            "top_10_hit_props": [_hit_prop_line(prop) for prop in hit_props],
            "top_10_hr_watch": [_hr_watch_line(prop) for prop in hr_watch],
            "top_10_strikeout_props": [_strikeout_prop_line(prop) for prop in strikeouts],
            "top_10_team_totals": mlb_top5.get("team_totals") or [],
            "top_10_game_totals": mlb_top5.get("game_totals") or [],
            "top_10_moneylines": mlb_top5.get("moneylines") or [],
            "top_10_f5_moneylines": mlb_top5.get("f5_moneylines") or [],
            "top_underdogs": _top_underdogs(card_date),
        },
        "soccer_top_opportunities": {
            "candidate_count": soccer_count,
        },
        "debug": {
            "mlb_games_scanned": len(slate),
            "mlb_hitters_scanned": int(props_debug.get("players_scanned") or 0),
            "projected_lineups_found": int(streak_debug.get("projected_lineups_count") or 0),
            "confirmed_lineups_found": int(streak_debug.get("confirmed_lineups_count") or 0),
            "props_candidates_created": int(props_debug.get("candidate_props_created") or 0),
            "matched_odds_games": matched_odds_games,
            "props_rejected": props_debug.get("rejected_props") or [],
            "props_rejected_reason": (
                (props_debug.get("rejected_props") or ["No rejected props logged."])[0]
                if isinstance(props_debug.get("rejected_props") or [], list)
                else "No rejected props logged."
            ),
            "soccer_candidates_count": soccer_count,
            "data_sources_used": props_debug.get("data_sources_used") or {},
            "prop_missing_reasons": {
                "hits": "" if hit_props else _prop_empty_reason(props, "hits"),
                "home_runs": "" if hr_watch else _prop_empty_reason(props, "home_runs"),
                "strikeouts": "" if strikeouts else _prop_empty_reason(props, "strikeouts"),
            },
            "market_debug": mlb_top5.get("market_debug") or {},
        },
        "player_trends": trends,
        "pitcher_reports": pitcher_reports,
        "bullpen_reports": bullpen_reports,
        "pitch_type_matchup_report": _pitch_type_report(slate),
        "weather_park_edge": _weather_park_edges(slate),
        "daily_ai_review": _daily_review(card_date),
        "errors": errors,
    }
    _write_json(_report_dir(card_date) / "intelligence_dashboard.json", dashboard)
    return dashboard


def render_daily_intel(payload: dict[str, Any]) -> str:
    """Render the full admin dashboard in a compact Telegram format."""
    health = payload.get("daily_health", {})
    opps = payload.get("mlb_top_opportunities", {}) or payload.get("top_opportunities", {})
    soccer = payload.get("soccer_top_opportunities", {})
    lines = [
        "🧠 BETGPTAI INTELLIGENCE DASHBOARD v1",
        f"📅 Date: {payload.get('display_date')}",
        "🧪 Admin Only",
        "",
        "━━━━━━━━━━━━",
        "DAILY HEALTH",
        f"Storage: {health.get('storage_status')}",
        f"MLB Games: {health.get('today_mlb_games')}",
        f"Verification Score: {health.get('verification_score', 0)}/100",
        f"Lineups: {health.get('lineup_status')}",
        f"Starting Pitchers: {health.get('starting_pitcher_status')}",
        f"Images: {health.get('images_status')}",
        f"Results Storage: {health.get('results_storage_status')}",
        "",
        "API Status:",
    ]
    for name, status in (health.get("apis_status") or {}).items():
        lines.append(f"- {name}: {status}")
    lines.extend(["", "━━━━━━━━━━━━", "⚾ MLB TOP OPPORTUNITIES"])
    for label, key in (
        ("Top Hit Props", "top_hit_props"),
        ("HR Watch", "hr_watch"),
        ("Strikeout Props", "strikeout_props"),
        ("Game Totals", "game_totals"),
        ("Team Totals", "team_totals"),
        ("Moneylines", "moneylines"),
        ("F5 Moneylines", "f5_moneylines"),
        ("Underdogs", "top_underdogs"),
    ):
        values = opps.get(key) or []
        lines.extend(["", label + ":"])
        if values:
            lines.extend(f"{idx}. {item}" for idx, item in enumerate(values[:10], start=1))
        else:
            reason_key = {
                "top_hit_props": "top_hit_props_reason",
                "hr_watch": "hr_watch_reason",
                "strikeout_props": "strikeout_props_reason",
                "game_totals": "game_totals_reason",
                "team_totals": "team_totals_reason",
                "moneylines": "moneylines_reason",
                "f5_moneylines": "f5_moneylines_reason",
            }.get(key)
            reason = opps.get(reason_key) if reason_key else "No qualified MLB candidates available from current engines."
            lines.append(str(reason or "No qualified MLB candidates available from current engines."))
    lines.extend([
        "",
        "━━━━━━━━━━━━",
        "⚽ SOCCER TOP OPPORTUNITIES",
        f"Soccer candidates count: {soccer.get('candidate_count', 0)}",
        "Soccer opportunities are separated from MLB and never mixed into MLB boards.",
    ])
    if payload.get("errors"):
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {item}" for item in payload["errors"][:8])
    return "\n".join(lines).strip()


def render_morning_report(payload: dict[str, Any]) -> str:
    """Render a morning owner report focused on readiness and opportunities."""
    health = payload.get("daily_health", {})
    opps = payload.get("mlb_top_opportunities", {}) or payload.get("top_opportunities", {})
    return (
        "🌅 BETGPTAI MORNING REPORT\n"
        f"📅 Date: {payload.get('display_date')}\n"
        "🧪 Admin Only\n\n"
        f"MLB Games: {health.get('today_mlb_games')}\n"
        f"Lineups: {health.get('lineup_status')}\n"
        f"Starting Pitchers: {health.get('starting_pitcher_status')}\n"
        f"Images: {health.get('images_status')}\n\n"
        "Top Hit Props:\n"
        + "\n".join(
            f"{i}. {item}" for i, item in enumerate((opps.get("top_hit_props") or [])[:5], 1)
        )
        + "\n\nTop Game Totals:\n"
        + "\n".join(f"{i}. {item}" for i, item in enumerate((opps.get("game_totals") or [])[:5], 1))
        + "\n\nTop Moneylines:\n"
        + "\n".join(f"{i}. {item}" for i, item in enumerate((opps.get("moneylines") or [])[:5], 1))
    ).strip()


def render_intel_debug(payload: dict[str, Any]) -> str:
    """Render owner-only Intelligence Dashboard debug details."""
    debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
    sources = debug.get("data_sources_used") if isinstance(debug.get("data_sources_used"), dict) else {}
    rejected = debug.get("props_rejected") if isinstance(debug.get("props_rejected"), list) else []
    missing = debug.get("prop_missing_reasons") if isinstance(debug.get("prop_missing_reasons"), dict) else {}
    market_debug = debug.get("market_debug") if isinstance(debug.get("market_debug"), dict) else {}
    opps = payload.get("mlb_top_opportunities", {}) or payload.get("top_opportunities", {})
    lines = [
        "🧪 BETGPTAI INTELLIGENCE DEBUG",
        f"📅 Date: {payload.get('display_date')}",
        "🧪 Admin Only",
        "",
        f"MLB games scanned: {debug.get('mlb_games_scanned', 0)}",
        f"MLB hitters scanned: {debug.get('mlb_hitters_scanned', 0)}",
        f"Projected lineups found: {debug.get('projected_lineups_found', 0)}",
        f"Confirmed lineups found: {debug.get('confirmed_lineups_found', 0)}",
        f"Props candidates created: {debug.get('props_candidates_created', 0)}",
        f"Soccer candidates count: {debug.get('soccer_candidates_count', 0)}",
        "",
    ]
    if market_debug:
        lines.extend(["── Market Debug ──"])
        lines.append(f"Market: {market_debug.get('market', 'N/A')}")
        lines.append(f"Candidates scanned: {market_debug.get('candidates_scanned', 0)}")
        lines.append(f"Overs created: {market_debug.get('overs_created', 0)}")
        lines.append(f"Unders created: {market_debug.get('unders_created', 0)}")
        lines.append(f"Rejected count: {market_debug.get('rejected_count', 0)}")
        lines.append(f"Edge threshold: {market_debug.get('edge_threshold_used', 'N/A')}")
        lines.append(f"Fallback used: {'yes' if market_debug.get('fallback_used') else 'no'}")
        reasons = market_debug.get("rejection_reasons") or []
        if reasons:
            lines.append("Rejection reasons:")
            lines.extend(f"- {r}" for r in reasons[:5])
        lines.append("")
    for market_name, market_key in (
        ("Game Totals", "game_totals"),
        ("Team Totals", "team_totals"),
        ("Moneylines", "moneylines"),
    ):
        items = opps.get(market_key) or []
        lines.append(f"── {market_name} ──")
        lines.append(f"Total: {len(items)}")
        inferred_count = sum(1 for it in items if "Inferred line" in str(it))
        lines.append(f"Inferred (admin-only): {inferred_count}")
        real_count = len(items) - inferred_count
        lines.append(f"Real market lines: {real_count}")
        lines.append("")
    lines.extend([
        "Data sources used:",
    ])
    if sources:
        lines.extend(f"- {key}: {value}" for key, value in sources.items())
    else:
        lines.append("- No props source details available.")
    lines.extend(["", "Props rejected and why:"])
    if rejected:
        lines.extend(f"- {item}" for item in rejected[:25])
    else:
        lines.append("- No rejected props logged.")
    if missing:
        lines.extend(["", "Empty prop board reasons:"])
        for key, value in missing.items():
            if value:
                lines.append(f"- {key}: {value}")
    return "\n".join(str(line) for line in lines).strip()


def render_lineup_report(payload: dict[str, Any]) -> str:
    """Render lineup and player-trend focused research."""
    trends = payload.get("player_trends", {})
    streak_payload = build_hitting_streak_report(payload["card_date"])
    return (
        render_hitting_streak_report(streak_payload)
        + "\n\n━━━━━━━━━━━━\n\n"
        + "MULTI-HIT TRENDS\n"
        + "\n".join(f"- {item}" for item in trends.get("multi_hit_trends", [])[:20])
    ).strip()


def render_model_review(payload: dict[str, Any]) -> str:
    """Render postgame model review."""
    review = payload.get("daily_ai_review", {})
    lines = [
        "🧾 BETGPTAI MODEL REVIEW",
        f"📅 Date: {payload.get('display_date')}",
        "🧪 Admin Only",
        "",
        f"Graded Picks: {review.get('graded_picks', 0)}",
        f"W-L-P: {review.get('wins', 0)}-{review.get('losses', 0)}-{review.get('pushes', 0)}",
        "",
        "Loss Tags:",
    ]
    losses = review.get("loss_reviews") or []
    if losses:
        lines.extend(f"- {item.get('pick')}: {item.get('tag')}" for item in losses[:20])
    else:
        lines.append("- No graded losses to review yet.")
    lines.append("")
    lines.append(f"Saved: {data_file('model_reviews') / (str(payload.get('card_date')) + '.json')}")
    return "\n".join(lines).strip()


def intelligence_dashboard_available() -> bool:
    """Lightweight status hook for /model_report."""
    return True
