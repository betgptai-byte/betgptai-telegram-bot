"""Admin-only BETGPTAI MLB Player Props Engine — Elite Admin v2.

Props stay private for now:

- No public menu entries.
- No Free/VIP/community posting.
- No official picks.json writes.
- Admin-tested props are saved only to props_lab.json.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from hitting_streaks import get_hitting_streak, hitting_streak_score_adjustment
from lineup_verification import verify_pitcher_prop_start_state, verify_prop_lineup_state
from player_verification import verify_player_team, verify_player_team_by_id
from premium_card_formatter import render_prop_block
from storage import data_file


PROPS_LAB_FILE = data_file("props_lab.json")
APPROVED_PROPS_FILE = data_file("approved_props.json")
EASTERN = ZoneInfo("America/New_York")
MLB_PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
REQUEST_TIMEOUT = 10

SUPPORTED_PROP_TYPES = (
    "hits",
    "2_plus_hits",
    "home_runs",
    "rbis",
    "runs",
    "total_bases",
    "walks",
    "strikeouts",
    "pitcher_outs_recorded",
    "earned_runs",
    "stolen_bases",
)
FINAL_RESULTS = {"win", "loss", "push"}


def player_props_engine_available() -> bool:
    """Lightweight owner-only status check."""
    return True


def _now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _num(value: Any) -> float | None:
    """Convert numbers and percent strings into floats."""
    if value in (None, "", "unavailable"):
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


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _metric_score(
    value: Any,
    *,
    average: float,
    weight: float,
    lower_is_better: bool = False,
) -> float:
    number = _num(value)
    if number is None:
        return 0.0
    edge = average - number if lower_is_better else number - average
    return max(-weight, min(weight, edge * weight))


def _american_implied_probability(odds: Any) -> float | None:
    price = _num(odds)
    if price is None or price == 0:
        return None
    return round(abs(price) / (abs(price) + 100), 4) if price < 0 else round(100 / (price + 100), 4)


def _projection_from_score(score: float) -> float:
    """Convert internal 0-100-ish score into a conservative probability."""
    probability = 0.48 + ((score - 50) / 100)
    return round(max(0.35, min(0.74, probability)), 4)


def _confidence(score: float) -> tuple[str, int]:
    """Return public admin tier and numeric grade. Below 6 should be rejected."""
    if score >= 82:
        return "9/10 Elite", 9
    if score >= 72:
        return "8/10 Strong", 8
    if score >= 62:
        return "7/10 Playable", 7
    if score >= 54:
        return "6/10 Lean", 6
    return "Below 6/10 — No Play", 5


def _prop_id(parts: list[Any]) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:18]


def _first_available(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", "unavailable", [], {}):
            return value
    return None


def _format_game_time_et(value: Any) -> str:
    """Format an MLB API timestamp as a simple Eastern Time display string."""
    if not isinstance(value, str) or not value.strip():
        return "Time unavailable ET"
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return "Time unavailable ET"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    eastern = parsed.astimezone(EASTERN)
    return f"{eastern.strftime('%I:%M %p').lstrip('0')} ET"


def _lookup_player_team_name(player_id: Any) -> str | None:
    """Best-effort MLB Stats API lookup used only when prop team data is missing."""
    if not player_id:
        return None
    try:
        response = requests.get(
            f"{MLB_PEOPLE_URL}/{player_id}",
            params={"hydrate": "currentTeam"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        people = response.json().get("people", [])
        if not people:
            return None
        current_team = people[0].get("currentTeam") or {}
        return current_team.get("name")
    except Exception:
        # Admin cards should never fail because a player metadata lookup fails.
        return None


def _ensure_prop_display_fields(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize old/new prop objects so admin cards always show team context."""
    if not item:
        return None
    if not isinstance(item, dict):
        return None
    if not item.get("team_name"):
        item["team_name"] = _first_available(
            item.get("team"),
            _lookup_player_team_name(item.get("player_id")),
            "Team unavailable",
        )
    if not item.get("opponent_name"):
        item["opponent_name"] = _first_available(item.get("opponent"), "Opponent unavailable")
    if not item.get("game_matchup"):
        raw_game = _dict(item.get("raw_game"))
        away_team = _first_available(raw_game.get("away_team"), item.get("away_team"))
        home_team = _first_available(raw_game.get("home_team"), item.get("home_team"))
        if away_team and home_team:
            item["game_matchup"] = f"{away_team} @ {home_team}"
        else:
            item["game_matchup"] = "Matchup unavailable"
    if not item.get("game_time_et"):
        item["game_time_et"] = _format_game_time_et(item.get("game_time"))
    # Preserve older keys for compatibility with any existing admin tools.
    item["team"] = item.get("team") or item.get("team_name")
    item["opponent"] = item.get("opponent") or item.get("opponent_name")
    return item


def _team_side(game: dict[str, Any], side: str) -> dict[str, Any]:
    """Collect away/home context from a combined MLB slate game."""
    opponent = "home" if side == "away" else "away"
    savant = _dict(game.get("savant"))
    fangraphs = _dict(game.get("fangraphs"))
    matchups = _dict(savant.get("pitch_type_matchups"))
    away_team = game.get("away_team")
    home_team = game.get("home_team")
    team_name = game.get(f"{side}_team") or game.get(f"{side}_team_name") or (away_team if side == "away" else home_team)
    opponent_name = game.get(f"{opponent}_team") or game.get(f"{opponent}_team_name") or (home_team if side == "away" else away_team)
    game_matchup = f"{away_team} @ {home_team}" if away_team and home_team else "Matchup unavailable"
    return {
        "side": side,
        "team": team_name,
        "team_name": team_name,
        "opponent": opponent_name,
        "opponent_name": opponent_name,
        "game_matchup": game_matchup,
        "game_pk": game.get("game_pk") or game.get("game_id"),
        "game_time": game.get("game_time"),
        "pitcher": game.get(f"{side}_pitcher"),
        "opposing_pitcher": game.get(f"{opponent}_pitcher"),
        "pitcher_stats": _dict(game.get(f"{side}_pitcher_stats")),
        "opposing_pitcher_stats": _dict(game.get(f"{opponent}_pitcher_stats")),
        "savant_batters": _list(savant.get(f"{side}_batters")),
        "savant_team": _dict(savant.get(f"{side}_team")),
        "opposing_savant_team": _dict(savant.get(f"{opponent}_team")),
        "pitcher_savant": _dict(savant.get(f"{side}_pitcher")),
        "opposing_pitcher_savant": _dict(savant.get(f"{opponent}_pitcher")),
        "opposing_bullpen": _dict(savant.get(f"{opponent}_bullpen")),
        "pitch_type_matchup": _dict(matchups.get(f"{opponent}_pitcher_vs_{side}")),
        "fangraphs_hitters": _list(fangraphs.get(f"{side}_hitter_samples")),
        "fangraphs_team_batting": _dict(fangraphs.get(f"{side}_team_batting")),
        "fangraphs_pitcher": _dict(fangraphs.get(f"{side}_pitcher")),
        "fangraphs_opposing_pitcher": _dict(fangraphs.get(f"{opponent}_pitcher")),
        "weather": _dict(game.get("weather")),
        "park_factor": str(game.get("park_factor") or game.get("park_factor_label") or "neutral"),
        "lineups_available": bool(game.get("lineups") not in (None, "", "unavailable", [], {})),
        "raw_game": game,
    }


def _player_name(row: dict[str, Any], fallback: str = "Player TBD") -> str:
    name = str(_first_available(row.get("player"), row.get("Name"), row.get("name"), fallback))
    if "," in name:
        last, first = [part.strip() for part in name.split(",", 1)]
        if first and last:
            return f"{first} {last}"
    return name


def _player_id(row: dict[str, Any]) -> Any:
    return _first_available(row.get("player_id"), row.get("id"), row.get("batter"), row.get("mlbam_id"))


def _hitter_score(row: dict[str, Any]) -> float:
    return (
        48
        + _metric_score(row.get("xBA"), average=0.250, weight=100)
        + _metric_score(row.get("xSLG"), average=0.410, weight=55)
        + _metric_score(row.get("xwOBA") or row.get("wOBA"), average=0.320, weight=115)
        + _metric_score(row.get("Barrel %"), average=8.0, weight=1.9)
        + _metric_score(row.get("Hard Hit %") or row.get("Hard%"), average=40.0, weight=0.55)
        + _metric_score(row.get("Exit Velocity"), average=88.0, weight=1.15)
        + _metric_score(row.get("Sweet Spot %"), average=33.0, weight=0.35)
        + _metric_score(row.get("Whiff %"), average=25.0, weight=0.35, lower_is_better=True)
        + _metric_score(row.get("Chase %"), average=28.0, weight=0.25, lower_is_better=True)
        + _metric_score(row.get("OPS"), average=0.720, weight=38)
        + _metric_score(row.get("ISO"), average=0.160, weight=70)
    )


def _hitter_pool(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Combine Savant and FanGraphs hitter samples without failing on missing data."""
    rows = context["savant_batters"] or context["fangraphs_hitters"]
    cleaned = []
    for row in rows:
        copy = dict(row)
        copy["_raw_hitter_score"] = round(_hitter_score(copy), 1)
        cleaned.append(copy)
    return sorted(cleaned, key=lambda item: item["_raw_hitter_score"], reverse=True)


def _pitcher_k_score(context: dict[str, Any]) -> float:
    savant = context["pitcher_savant"]
    fg = context["fangraphs_pitcher"]
    opponent = context["opposing_savant_team"]
    return (
        48
        + _metric_score(savant.get("Whiff %"), average=25.0, weight=0.9)
        + _metric_score(savant.get("Chase %"), average=28.0, weight=0.5)
        + _metric_score(fg.get("K%"), average=22.0, weight=0.8)
        + _metric_score(fg.get("K-BB%"), average=14.0, weight=0.75)
        + _metric_score(savant.get("xERA"), average=4.00, weight=3.5, lower_is_better=True)
        + _metric_score(opponent.get("xwOBA"), average=0.315, weight=50, lower_is_better=True)
        + _metric_score(opponent.get("Barrel %"), average=8.0, weight=0.65, lower_is_better=True)
    )


def _pitcher_prevention_score(context: dict[str, Any]) -> float:
    savant = context["pitcher_savant"]
    fg = context["fangraphs_pitcher"]
    stats = context["pitcher_stats"]
    return (
        50
        + _metric_score(savant.get("xERA"), average=4.00, weight=4.0, lower_is_better=True)
        + _metric_score(stats.get("whip") or stats.get("WHIP"), average=1.30, weight=9, lower_is_better=True)
        + _metric_score(savant.get("xBA"), average=0.240, weight=75, lower_is_better=True)
        + _metric_score(savant.get("xSLG"), average=0.410, weight=45, lower_is_better=True)
        + _metric_score(savant.get("Barrel %"), average=8.0, weight=1.4, lower_is_better=True)
        + _metric_score(savant.get("Hard Hit %"), average=40.0, weight=0.4, lower_is_better=True)
        + _metric_score(fg.get("BB%"), average=8.0, weight=0.45, lower_is_better=True)
    )


def _market_stub(prop_type: str) -> tuple[float | None, int | None]:
    """Odds props are optional; leave blank until a prop odds feed is added."""
    del prop_type
    return None, None


def _ev_fields(score: float, odds: int | None) -> tuple[float, float | None, float | None, str]:
    projected = _projection_from_score(score)
    implied = _american_implied_probability(odds)
    if implied is None:
        return projected, None, None, "Model lean — odds not verified."
    edge = round(projected - implied, 4)
    value_note = "+EV candidate" if edge > 0 else "No verified value edge."
    return projected, implied, edge, value_note


def _reason(parts: list[str]) -> str:
    return " ".join(part for part in parts if part).strip()


def _add_reason_count(counts: dict[str, int], reason: Any) -> None:
    """Track compact rejection reason counts for owner diagnostics."""
    key = str(reason or "unknown").strip() or "unknown"
    key = key.split(":", 1)[0]
    counts[key] = counts.get(key, 0) + 1


def _verify_player_for_prop(
    prop: dict[str, Any],
    slate: list[dict[str, Any]],
    reason_counts: dict[str, int],
) -> tuple[bool, str]:
    """Verify one prop candidate without ever stopping the whole engine.

    MLB Stats API active-roster verification is the source of truth. Baseball
    Savant/FanGraphs are enrichment only and can never veto an MLB-confirmed
    active player.
    """
    expected_team = str(prop.get("team_name") or prop.get("team") or "")
    player_id = prop.get("player_id")
    if player_id:
        verification = verify_player_team_by_id(player_id, expected_team)
    else:
        verification = verify_player_team(str(prop.get("player_name") or ""), expected_team)
    prop["player_verification"] = verification
    if verification.get("player_id") and not prop.get("player_id"):
        prop["player_id"] = verification.get("player_id")
    if verification.get("player_name"):
        prop["player_name"] = verification.get("player_name")
    if verification.get("current_team"):
        prop["team_name"] = verification.get("current_team")
        prop["team"] = verification.get("current_team")
    if not verification.get("verified") or not verification.get("active_roster", True):
        reason = verification.get("reason") or verification.get("status") or "MLB active roster verification failed"
        _add_reason_count(reason_counts, verification.get("status") or reason)
        return False, str(reason)

    pitcher_types = {"strikeouts", "pitcher_outs_recorded", "earned_runs", "hits_allowed"}
    pitcher_check = verify_pitcher_prop_start_state(prop, slate)
    if prop.get("prop_type") in pitcher_types:
        prop["pitcher_verification"] = pitcher_check
        if not pitcher_check.get("verified"):
            reason = pitcher_check.get("reason") or "Starting pitcher verification failed"
            _add_reason_count(reason_counts, pitcher_check.get("status") or reason)
            return False, str(reason)
        return True, ""
    if pitcher_check.get("verified"):
        prop["pitcher_verification"] = pitcher_check
        return True, ""

    hitter_types = {
        "hits", "2_plus_hits", "home_runs", "rbis", "runs",
        "total_bases", "walks", "stolen_bases",
    }
    if prop.get("prop_type") in hitter_types:
        lineup_state = verify_prop_lineup_state(prop, slate)
        prop["lineup_verification"] = lineup_state
        if lineup_state.get("state") == "Scratched":
            reason = lineup_state.get("reason") or "Player scratched/not in confirmed lineup"
            _add_reason_count(reason_counts, "scratched")
            return False, str(reason)
        if not lineup_state.get("verified"):
            reason = lineup_state.get("reason") or "Lineup verification failed"
            _add_reason_count(reason_counts, lineup_state.get("status") or reason)
            return False, str(reason)
    return True, ""


def _base_prop(
    *,
    card_date: str,
    context: dict[str, Any],
    player_name: str,
    player_id: Any,
    prop_type: str,
    market_type: str,
    line: float | None,
    odds: int | None,
    score: float,
    reason: str,
) -> dict[str, Any] | None:
    confidence, grade = _confidence(score)
    if grade < 6:
        return None
    projected, implied, edge, value_note = _ev_fields(score, odds)
    prop_id = _prop_id([
        card_date, context.get("game_pk"), player_id or player_name,
        prop_type, line, odds,
    ])
    return {
        "prop_id": prop_id,
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "sport": "mlb",
        "game_pk": context.get("game_pk"),
        "player_id": player_id,
        "player_name": player_name,
        "team_name": context.get("team_name") or context.get("team"),
        "opponent_name": context.get("opponent_name") or context.get("opponent"),
        "game_matchup": context.get("game_matchup") or "Matchup unavailable",
        "game_time": context.get("game_time"),
        "game_time_et": _format_game_time_et(context.get("game_time")),
        "team": context.get("team"),
        "opponent": context.get("opponent"),
        "prop_type": prop_type,
        "market_type": market_type,
        "line": line,
        "odds": odds,
        "projected_probability": projected,
        "implied_probability": implied,
        "edge": edge,
        "confidence_grade": confidence,
        "reason": f"{reason} {value_note}",
        "status": "admin_preview",
        "result": "pending",
        "created_at": _now_iso(),
        "graded_at": None,
        "raw_score": round(score, 1),
        "value_note": value_note,
        "debug_context": {
            "park_factor": context.get("park_factor"),
            "weather": context.get("weather"),
            "lineups_available": context.get("lineups_available"),
            "opposing_pitcher": context.get("opposing_pitcher"),
        },
        "projected_batting_position": None,
    }


def _build_hitter_props(context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Create hitter prop candidates for one team side."""
    created: list[dict[str, Any]] = []
    rejected: list[str] = []
    hitters = _hitter_pool(context)
    if not hitters:
        return [], [f"{context.get('team')}: no hitter samples from Savant/FanGraphs"]

    pitcher = context["opposing_pitcher_savant"]
    bullpen = context["opposing_bullpen"]
    team = context["savant_team"]
    weather = context["weather"]
    park = str(context["park_factor"]).lower()
    park_boost = 4 if any(word in park for word in ("hitter", "hr", "extreme")) else 0
    wind_boost = 2 if (_num(weather.get("wind_speed")) or 0) >= 10 else 0

    for lineup_index, hitter in enumerate(hitters[:5], start=1):
        name = _player_name(hitter)
        pid = _player_id(hitter)
        base = hitter.get("_raw_hitter_score", 50)
        streak = get_hitting_streak(
            pid,
            name,
            str(context.get("team_name") or context.get("team") or ""),
        )
        streak_adjustment = hitting_streak_score_adjustment(streak)
        contact_score = (
            base
            + _metric_score(pitcher.get("xBA"), average=0.240, weight=85)
            + _metric_score(pitcher.get("Hard Hit %"), average=40.0, weight=0.35)
            + _metric_score(team.get("xwOBA"), average=0.315, weight=65)
            + _metric_score(bullpen.get("WHIP"), average=1.30, weight=7)
            + park_boost
            + streak_adjustment
        )
        reason = _reason([
            "Projected top-half bat with contact indicators.",
            f"xwOBA {hitter.get('xwOBA')}." if hitter.get("xwOBA") not in (None, "unavailable") else "",
            f"HardHit {hitter.get('Hard Hit %')}%." if hitter.get("Hard Hit %") not in (None, "unavailable") else "",
            (
                f"Active {streak.get('games_with_hit_streak')}-game hit streak."
                if streak.get("available") and streak.get("games_with_hit_streak", 0) >= 3
                else ""
            ),
            (
                f"Hit in {streak.get('hit_rate_last_10')}."
                if streak.get("available") and streak.get("hit_games_last_10", 0) >= 7
                else ""
            ),
            "Opposing starter shows elevated contact risk." if pitcher else "",
            "Park/weather adds support." if park_boost or wind_boost else "",
        ])
        for prop_type, market_type, line, modifier in (
            ("hits", "hits", 0.5, 0),
            ("2_plus_hits", "hits", 1.5, -9),
            ("total_bases", "total_bases", 1.5, 4),
            ("runs", "runs", 0.5, -1),
            ("rbis", "rbis", 0.5, -3),
        ):
            _, odds = _market_stub(prop_type)
            prop = _base_prop(
                card_date=context["card_date"], context=context,
                player_name=name, player_id=pid, prop_type=prop_type,
                market_type=market_type, line=line, odds=odds,
                score=contact_score + modifier,
                reason=reason or "Batter profile supports contact upside.",
            )
            if prop:
                prop["projected_batting_position"] = lineup_index
                prop["hitting_streak"] = streak
                prop["hitting_streak_adjustment"] = streak_adjustment
                prop["savant_verification"] = {
                    "verified": any(
                        hitter.get(field) not in (None, "", "unavailable")
                        for field in ("xBA", "Hard Hit %", "Barrel %", "xwOBA")
                    ),
                    "xBA": hitter.get("xBA"),
                    "hard_hit_pct": hitter.get("Hard Hit %"),
                    "barrel_pct": hitter.get("Barrel %"),
                    "xwOBA": hitter.get("xwOBA"),
                    "recent_contact_profile": "available",
                    "reason": "Baseball Savant hitter contact metrics were available.",
                }
                prop["lineup_verification"] = {
                    "verified": True,
                    "state": "Projected",
                    "status": "projected",
                    "lineup_spot": lineup_index,
                    "reason": (
                        "Confirmed lineup data available."
                        if context.get("lineups_available")
                        else "Projected top-five lineup candidate from available hitter pool."
                    ),
                }
                created.append(prop)
            else:
                rejected.append(f"{name} {prop_type}: below 6/10 threshold")

        hr_score = (
            base
            + _metric_score(hitter.get("Barrel %"), average=8.0, weight=2.2)
            + _metric_score(hitter.get("Exit Velocity"), average=88.0, weight=1.25)
            + _metric_score(pitcher.get("Barrel %"), average=8.0, weight=1.6)
            + _metric_score(pitcher.get("xSLG"), average=0.410, weight=40)
            + park_boost + wind_boost + max(0, streak_adjustment * 0.35) - 10
        )
        hr_reason = _reason([
            "Power profile supports HR watch.",
            f"Barrel {hitter.get('Barrel %')}%." if hitter.get("Barrel %") not in (None, "unavailable") else "",
            f"EV {hitter.get('Exit Velocity')} mph." if hitter.get("Exit Velocity") not in (None, "unavailable") else "",
            "Pitcher allows power contact." if pitcher else "",
            "Run environment helps carry." if park_boost or wind_boost else "",
        ])
        prop = _base_prop(
            card_date=context["card_date"], context=context,
            player_name=name, player_id=pid, prop_type="home_runs",
            market_type="home_runs", line=0.5, odds=None,
            score=hr_score, reason=hr_reason or "Power indicators create a HR watch spot.",
        )
        if prop:
            prop["projected_batting_position"] = lineup_index
            prop["hitting_streak"] = streak
            prop["hitting_streak_adjustment"] = streak_adjustment
            prop["savant_verification"] = {
                "verified": any(
                    hitter.get(field) not in (None, "", "unavailable")
                    for field in ("xSLG", "Barrel %", "Hard Hit %", "exit_velocity")
                ),
                "xSLG": hitter.get("xSLG"),
                "hard_hit_pct": hitter.get("Hard Hit %"),
                "barrel_pct": hitter.get("Barrel %"),
                "exit_velocity": hitter.get("exit_velocity"),
                "recent_contact_profile": "available",
                "reason": "Baseball Savant power/contact metrics were available.",
            }
            prop["lineup_verification"] = {
                "verified": True,
                "state": "Projected",
                "status": "projected",
                "lineup_spot": lineup_index,
                "reason": (
                    "Confirmed lineup data available."
                    if context.get("lineups_available")
                    else "Projected top-five lineup candidate from available hitter pool."
                ),
            }
            created.append(prop)

        sb_score = contact_score - 10
        prop = _base_prop(
            card_date=context["card_date"], context=context,
            player_name=name, player_id=pid, prop_type="stolen_bases",
            market_type="stolen_bases", line=0.5, odds=None,
            score=sb_score,
            reason="Speed/OBP proxy creates a stolen-base watch, but lineup and catcher data should be confirmed.",
        )
        if prop:
            prop["projected_batting_position"] = lineup_index
            prop["hitting_streak"] = streak
            prop["hitting_streak_adjustment"] = streak_adjustment
            created.append(prop)
    return created, rejected


def _build_pitcher_props(context: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Create pitcher prop candidates for one starting pitcher."""
    pitcher = str(context.get("pitcher") or "")
    if not pitcher or pitcher == "TBD":
        return [], [f"{context.get('team')}: probable pitcher unavailable"]
    pitcher_savant = context["pitcher_savant"]
    fg_pitcher = context["fangraphs_pitcher"]
    pitcher_stats = context["pitcher_stats"]
    created: list[dict[str, Any]] = []
    rejected: list[str] = []

    k_score = _pitcher_k_score(context)
    if k_score < 54:
        k_score = 56
    k_reason = _reason([
        "Swing-and-miss profile supports strikeout upside.",
        f"Whiff {pitcher_savant.get('Whiff %')}%." if pitcher_savant.get("Whiff %") not in (None, "unavailable") else "",
        f"Chase {pitcher_savant.get('Chase %')}%." if pitcher_savant.get("Chase %") not in (None, "unavailable") else "",
        f"K-BB {fg_pitcher.get('K-BB%')}%." if fg_pitcher.get("K-BB%") not in (None, "unavailable") else "",
        "Probable starter fallback lean; confirm market line before use." if not pitcher_savant and not fg_pitcher else "",
    ])
    for prop_type, market_type, line, modifier, reason in (
        ("strikeouts", "strikeouts", 4.5, 0, k_reason),
        ("walks", "walks", 2.5, -5, "Command profile creates a walk lean; confirm umpire and opponent chase rates."),
    ):
        prop = _base_prop(
            card_date=context["card_date"], context=context,
            player_name=pitcher, player_id=context["raw_game"].get(f"{context['side']}_pitcher_id"),
            prop_type=prop_type, market_type=market_type, line=line, odds=None,
            score=k_score + modifier, reason=reason,
        )
        if prop:
            created.append(prop)
        else:
            rejected.append(f"{pitcher} {prop_type}: below 6/10 threshold")

    prevention_score = _pitcher_prevention_score(context)
    outs_score = prevention_score + _metric_score(fg_pitcher.get("BB%"), average=8.0, weight=0.4, lower_is_better=True)
    prevention_reason = _reason([
        "Starter profile supports run prevention and length.",
        f"xERA {pitcher_savant.get('xERA')}." if pitcher_savant.get("xERA") not in (None, "unavailable") else "",
        f"WHIP {pitcher_stats.get('whip') or pitcher_stats.get('WHIP')}." if pitcher_stats else "",
        "Contact suppression profile is favorable." if pitcher_savant else "",
    ])
    for prop_type, market_type, line, score, reason in (
        ("pitcher_outs_recorded", "pitcher_outs_recorded", 15.5, outs_score, prevention_reason),
        ("earned_runs", "earned_runs", 2.5, prevention_score, prevention_reason),
    ):
        prop = _base_prop(
            card_date=context["card_date"], context=context,
            player_name=pitcher, player_id=context["raw_game"].get(f"{context['side']}_pitcher_id"),
            prop_type=prop_type, market_type=market_type, line=line, odds=None,
            score=score, reason=reason,
        )
        if prop:
            created.append(prop)
    return created, rejected


def build_player_props_lab(slate: list[dict[str, Any]], card_date: str) -> dict[str, Any]:
    """Build and save the admin-only Elite v2 props lab payload."""
    all_props: list[dict[str, Any]] = []
    rejected: list[str] = []
    verification_issues: list[str] = []
    reason_counts: dict[str, int] = {}
    players_scanned = 0
    pitchers_scanned = 0
    games_scanned = len(slate)

    for game in slate:
        for side in ("away", "home"):
            context = _team_side(game, side)
            context["card_date"] = card_date
            hitters = _hitter_pool(context)
            players_scanned += len(hitters)
            if context.get("pitcher") and context.get("pitcher") != "TBD":
                pitchers_scanned += 1
            hitter_props, hitter_rejected = _build_hitter_props(context)
            pitcher_props, pitcher_rejected = _build_pitcher_props(context)
            all_props.extend(hitter_props + pitcher_props)
            rejected.extend(hitter_rejected + pitcher_rejected)

    top_candidates_before_filter = sorted(
        all_props,
        key=lambda item: item.get("raw_score", 0),
        reverse=True,
    )[:25]
    raw_candidate_count = len(all_props)
    verified_props: list[dict[str, Any]] = []
    for prop in all_props:
        ok, reason = _verify_player_for_prop(prop, slate, reason_counts)
        if ok:
            verified_props.append(prop)
        else:
            verification_issues.append(
                f"{prop.get('player_name')} removed from {prop.get('prop_type')}: "
                f"{reason}"
            )
    all_props = verified_props
    all_props = sorted(all_props, key=lambda item: item.get("raw_score", 0), reverse=True)
    grouped = {
        prop_type: [prop for prop in all_props if prop.get("prop_type") == prop_type]
        for prop_type in SUPPORTED_PROP_TYPES
    }
    teams_playing: list[str] = []
    for game in slate:
        for team in (game.get("away_team"), game.get("home_team")):
            if team and team not in teams_playing:
                teams_playing.append(str(team))
    hits_by_team = {
        team: next((prop for prop in grouped["hits"] if prop.get("team") == team), None)
        for team in teams_playing
    }
    missing_data = []
    if not any(isinstance(game.get("savant"), dict) for game in slate):
        missing_data.append("Baseball Savant")
    if not any(isinstance(game.get("fangraphs"), dict) for game in slate):
        missing_data.append("FanGraphs optional")
    if not any(game.get("lineups") not in (None, "", "unavailable", [], {}) for game in slate):
        missing_data.append("Confirmed lineups")
    if not any(game.get("weather") not in (None, "", "unavailable", {}, []) for game in slate):
        missing_data.append("Weather")
    if not any(
        isinstance(prop.get("hitting_streak"), dict)
        and prop["hitting_streak"].get("available")
        for prop in all_props
    ):
        missing_data.append("Hitting streak game logs")

    payload = {
        "engine_version": "Elite Admin v2",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": _now_iso(),
        "supported_prop_types": list(SUPPORTED_PROP_TYPES),
        "best_hit": (grouped["hits"] or [None])[0],
        "best_two_hit": (grouped["2_plus_hits"] or [None])[0],
        "hr_watch": (grouped["home_runs"] or [None])[0],
        "best_strikeout": (grouped["strikeouts"] or [None])[0],
        "hits_by_team": hits_by_team,
        "teams_playing": teams_playing,
        "all_props": all_props,
        "candidates": grouped,
        "debug": {
            "data_sources_used": {
                "mlb_stats_api": bool(slate),
                "baseball_savant": any(isinstance(game.get("savant"), dict) for game in slate),
                "fangraphs_optional": any(isinstance(game.get("fangraphs"), dict) for game in slate),
                "odds_props_optional": any(prop.get("odds") is not None for prop in all_props),
                "weather": "Weather" not in missing_data,
                "lineups": "Confirmed lineups" not in missing_data,
                "hitting_streaks": "Hitting streak game logs" not in missing_data,
            },
            "games_scanned": games_scanned,
            "players_scanned": players_scanned,
            "total_hitters_scanned": players_scanned,
            "valid_hitters": len({
                str(prop.get("player_id") or prop.get("player_name"))
                for prop in all_props
                if prop.get("prop_type") in {"hits", "2_plus_hits", "home_runs", "rbis", "runs", "total_bases", "walks", "stolen_bases"}
            }),
            "rejected_hitters": len([
                item for item in verification_issues
                if any(prop_type in item for prop_type in ("hits", "home_runs", "rbis", "runs", "total_bases", "walks", "stolen_bases"))
            ]),
            "reason_counts": reason_counts,
            "starting_pitchers_scanned": pitchers_scanned,
            "raw_candidate_props_created": raw_candidate_count,
            "candidate_props_created": len(all_props),
            "final_props_created": len(all_props),
            "rejected_props": (verification_issues + rejected)[:50],
            "player_verification_issues": verification_issues[:50],
            "top_candidates_before_filter": top_candidates_before_filter[:15],
            "top_raw_candidates": all_props[:15],
            "missing_fields": missing_data,
        },
        "source_status": {
            "mlb_stats_api": bool(slate),
            "baseball_savant": any(isinstance(game.get("savant"), dict) for game in slate),
            "fangraphs": any(isinstance(game.get("fangraphs"), dict) for game in slate),
            "odds_api_props": any(prop.get("odds") is not None for prop in all_props),
            "weather": "Weather" not in missing_data,
            "lineups": "Confirmed lineups" not in missing_data,
            "hitting_streaks": "Hitting streak game logs" not in missing_data,
        },
    }
    _save_props_lab(payload)
    return payload


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _save_props_lab(payload: dict[str, Any]) -> None:
    # Props Lab is a same-day cache only. Keeping yesterday's prop pool around
    # is how stale names can accidentally reappear in public-facing paths.
    _write_json(PROPS_LAB_FILE, {payload["card_date"]: payload})


def remove_prop_from_today_cache(card_date: str, prop: dict[str, Any] | None, reason: str = "") -> None:
    """Remove a rejected prop from today's cache so it cannot be reused later."""
    if not isinstance(prop, dict):
        return
    cache = _read_json(PROPS_LAB_FILE, {})
    if not isinstance(cache, dict):
        return
    payload = cache.get(card_date)
    if not isinstance(payload, dict):
        return
    prop_id = prop.get("prop_id")
    player_id = str(prop.get("player_id") or "")
    player_name = str(prop.get("player_name") or "")

    def same(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False
        if prop_id and candidate.get("prop_id") == prop_id:
            return True
        if player_id and str(candidate.get("player_id") or "") == player_id:
            return True
        return bool(player_name and candidate.get("player_name") == player_name)

    for key in ("all_props",):
        if isinstance(payload.get(key), list):
            payload[key] = [candidate for candidate in payload[key] if not same(candidate)]
    candidates = payload.get("candidates")
    if isinstance(candidates, dict):
        for key, values in list(candidates.items()):
            if isinstance(values, list):
                candidates[key] = [candidate for candidate in values if not same(candidate)]
    for key in ("best_hit", "best_two_hit", "hr_watch", "best_strikeout"):
        if same(payload.get(key)):
            payload[key] = None
    hits_by_team = payload.get("hits_by_team")
    if isinstance(hits_by_team, dict):
        for team, value in list(hits_by_team.items()):
            if same(value):
                hits_by_team[team] = None
    debug = payload.setdefault("debug", {})
    if isinstance(debug, dict):
        rejected = debug.setdefault("rejected_props", [])
        if isinstance(rejected, list):
            rejected.insert(0, f"{player_name or prop_id} removed from cache: {reason}")
    cache[card_date] = payload
    _write_json(PROPS_LAB_FILE, cache)


def _prop_display(item: dict[str, Any] | None) -> str:
    """Turn stored prop fields into a clean admin-facing betting label."""
    if not item:
        return "Unavailable"
    prop_type = str(item.get("prop_type") or item.get("market_type") or "").lower()
    line = item.get("line")
    over_label = {
        "hits": "Hits",
        "2_plus_hits": "Hits",
        "rbis": "RBIs",
        "runs": "Runs",
        "total_bases": "Total Bases",
        "walks": "Walks",
        "strikeouts": "Strikeouts",
        "pitcher_outs_recorded": "Pitcher Outs Recorded",
        "stolen_bases": "Stolen Bases",
    }
    if prop_type == "home_runs":
        return "HR Watch"
    if prop_type == "earned_runs":
        return f"Under {line} Earned Runs" if line is not None else "Earned Runs Lean"
    if prop_type in over_label:
        return f"Over {line} {over_label[prop_type]}" if line is not None else over_label[prop_type]
    market = str(item.get("market_type") or "Prop").replace("_", " ").title()
    return f"Over {line} {market}" if line is not None else market


def _hitting_streak_lines(item: dict[str, Any]) -> list[str]:
    """Return admin-only hitting-streak display lines for hitter props."""
    if str(item.get("market_type") or "").lower() not in {
        "hits",
        "home_runs",
        "total_bases",
        "rbis",
        "runs",
        "stolen_bases",
    }:
        return []
    streak = item.get("hitting_streak")
    if not isinstance(streak, dict) or not streak.get("available"):
        return [
            "Hit Streak: Unavailable",
            "Last 10 Hit Rate: Unavailable",
        ]
    games = _num(streak.get("games_with_hit_streak")) or 0
    games_text = f"{int(games)} game" if int(games) == 1 else f"{int(games)} games"
    return [
        f"Hit Streak: {games_text}",
        f"Last 10 Hit Rate: {streak.get('hit_rate_last_10', 'Unavailable')}",
    ]


def approve_prop(prop_id: str) -> tuple[bool, str]:
    """Move one lab prop into approved_props.json without publishing it."""
    lab = _read_json(PROPS_LAB_FILE, {})
    if not isinstance(lab, dict):
        return False, "props_lab.json is empty or invalid."
    found: dict[str, Any] | None = None
    for day_payload in lab.values():
        if not isinstance(day_payload, dict):
            continue
        for prop in day_payload.get("all_props", []):
            if isinstance(prop, dict) and prop.get("prop_id") == prop_id:
                found = dict(prop)
                break
        if found:
            break
    if not found:
        return False, f"Prop ID not found: {prop_id}"
    found["status"] = "approved_admin_only"
    found["approved_at"] = _now_iso()
    approved = _read_json(APPROVED_PROPS_FILE, {})
    if not isinstance(approved, dict):
        approved = {}
    approved[found["prop_id"]] = found
    _write_json(APPROVED_PROPS_FILE, approved)
    return True, f"Approved prop saved to approved_props.json: {prop_id}"


def _format_prop(item: dict[str, Any] | None, label: str, player_label: str = "Player") -> str:
    if not item:
        return (
            f"{label}\n"
            "\n"
            "👤 Unavailable\n"
            "🧢 Team unavailable\n"
            "🆚 Opponent unavailable\n"
            "🕒 Time unavailable ET\n\n"
            "🎯 Prop:\n"
            "Unavailable\n\n"
            "⭐ Confidence:\n"
            "N/A\n\n"
            "📈 Why:\n"
            "Not enough verified prop context."
        )
    item = _ensure_prop_display_fields(item) or item
    del player_label
    display = dict(item)
    display["pick_text"] = f"{display.get('player_name')} — {_prop_display(display)}"
    return render_prop_block(display, label=label)


def render_props_admin_card(payload: dict[str, Any]) -> str:
    """Render the clean admin-only props preview."""
    return (
        "⚾ BETGPTAI PLAYER PROP LAB\n\n"
        f"📅 Card Date: {payload.get('display_date')}\n"
        "🧪 Admin Preview Only\n\n"
        f"{_format_prop(payload.get('best_hit'), '🔥 BEST HIT PROP')}\n\n"
        f"{_format_prop(payload.get('best_two_hit'), '🔥 BEST 2+ HIT PROP')}\n\n"
        f"{_format_prop(payload.get('hr_watch'), '💣 HR WATCH')}\n\n"
        f"{_format_prop(payload.get('best_strikeout'), '🎯 BEST STRIKEOUT PROP', 'Pitcher')}\n\n"
        "━━━━━━━━━━━━\n\n"
        "⚠️ Admin-only test card.\n"
        "Not posted to members."
    )


def render_prop_type_card(payload: dict[str, Any], prop_type: str) -> str:
    """Render an admin-only list for one prop type."""
    candidates = payload.get("candidates", {})
    if prop_type == "hits":
        # /hits_admin should show both standard hit props and 2+ hit upside looks.
        items = list(candidates.get("hits", [])) + list(candidates.get("2_plus_hits", []))
    else:
        items = candidates.get(prop_type, [])
    if not isinstance(items, list):
        items = []
    title = {
        "hits": "🔥 BETGPTAI HIT / 2+ HIT PROP LAB",
        "2_plus_hits": "🔥 BETGPTAI 2+ HIT PROP LAB",
        "home_runs": "💣 BETGPTAI HR WATCH LAB",
        "strikeouts": "🎯 BETGPTAI STRIKEOUT PROP LAB",
    }.get(prop_type, "⚾ BETGPTAI PROP LAB")
    lines = [
        title,
        "",
        f"📅 Card Date: {payload.get('display_date')}",
        "🧪 Admin Preview Only",
        "",
    ]
    for index, item in enumerate(items[:10], start=1):
        item = _ensure_prop_display_fields(item) or item
        display = dict(item)
        display["pick_text"] = f"{display.get('player_name')} — {_prop_display(display)}"
        lines.append(render_prop_block(display, label=f"PROP ID: {item.get('prop_id')}", rank=index))
    if not items:
        lines.append("No qualified candidates available from current data.")
    lines.extend(["━━━━━━━━━━━━", "", "⚠️ Admin-only test card. Not posted to members."])
    return "\n".join(lines).strip()


def render_hits_by_team_card(payload: dict[str, Any]) -> str:
    """Render the best hit prop candidate for each MLB team playing today."""
    teams = payload.get("teams_playing") or []
    hits_by_team = payload.get("hits_by_team") or {}
    lines = [
        "⚾ BETGPTAI HITS BY TEAM LAB",
        f"📅 Card Date: {payload.get('display_date')}",
        "🧪 Admin Preview Only",
        "",
    ]
    for team in teams:
        item = hits_by_team.get(team)
        lines.append(f"{team}:")
        if item:
            item = _ensure_prop_display_fields(item) or item
            streak_lines = _hitting_streak_lines(item)
            entry_lines = [
                f"👤 {item.get('player_name')}",
                f"🧢 {item.get('team_name')}",
                f"🆚 {item.get('opponent_name')}",
                f"🕒 {item.get('game_time_et')}",
                "",
                "🎯 Prop:",
                _prop_display(item),
                "",
                "⭐ Confidence:",
                str(item.get("confidence_grade")),
                "",
                "📈 Why:",
                str(item.get("reason")),
                f"Prop ID: {item.get('prop_id')}",
                "",
            ]
            if streak_lines:
                entry_lines[8:8] = [*streak_lines, ""]
            lines.extend(entry_lines)
        else:
            lines.extend(["No qualified hit prop found.", ""])
    if not teams:
        lines.append("No MLB teams found on today’s slate.")
    lines.extend(["━━━━━━━━━━━━", "", "⚠️ Admin-only test card. Not posted to members."])
    return "\n".join(lines).strip()


def render_prop_debug(payload: dict[str, Any]) -> str:
    """Render raw admin debug details for candidate scoring."""
    debug = payload.get("debug", {})
    sources = debug.get("data_sources_used", {})
    lines = [
        "🧪 BETGPTAI PROP DEBUG",
        "",
        f"Card Date: {payload.get('display_date')}",
        "",
        "Data sources used:",
        f"- MLB Stats API: {'✅' if sources.get('mlb_stats_api') else '❌'}",
        f"- Baseball Savant: {'✅' if sources.get('baseball_savant') else '❌'}",
        f"- FanGraphs optional: {'✅' if sources.get('fangraphs_optional') else '❌'}",
        f"- Odds props optional: {'✅' if sources.get('odds_props_optional') else '❌'}",
        f"- Weather: {'✅' if sources.get('weather') else '❌'}",
        f"- Lineups: {'✅' if sources.get('lineups') else '❌'}",
        f"- Hitting streaks: {'✅' if sources.get('hitting_streaks') else '❌'}",
        "",
        f"Players scanned: {debug.get('players_scanned', 0)}",
        f"Starting pitchers scanned: {debug.get('starting_pitchers_scanned', 0)}",
        f"Raw candidates before filtering: {debug.get('raw_candidate_props_created', 0)}",
        f"Candidate props created: {debug.get('candidate_props_created', 0)}",
        "",
        "Rejected props with reasons:",
    ]
    rejected = debug.get("rejected_props") or []
    lines.extend(f"- {item}" for item in rejected[:15])
    if not rejected:
        lines.append("- None")
    lines.extend(["", "Top raw candidates:"])
    for item in (debug.get("top_raw_candidates") or [])[:10]:
        lines.append(
            f"- {item.get('player_name')} | {item.get('prop_type')} | "
            f"score={item.get('raw_score')} | grade={item.get('confidence_grade')} | "
            f"proj={item.get('projected_probability')} | edge={item.get('edge')}"
        )
    lines.extend(["", "Missing fields:"])
    missing = debug.get("missing_fields") or []
    lines.extend(f"- {item}" for item in missing)
    if not missing:
        lines.append("- None")
    return "\n".join(lines).strip()


def render_hitprops_debug(payload: dict[str, Any]) -> str:
    """Render focused owner-only Hit Props Engine diagnostics."""
    debug = payload.get("debug", {}) if isinstance(payload, dict) else {}
    candidates = payload.get("candidates", {}) if isinstance(payload, dict) else {}
    reason_counts = debug.get("reason_counts") if isinstance(debug.get("reason_counts"), dict) else {}
    top_before = debug.get("top_candidates_before_filter") or []
    hit_families = ("hits", "2_plus_hits", "home_runs", "rbis", "total_bases")
    final_count = sum(
        len(candidates.get(prop_type, []))
        for prop_type in hit_families
        if isinstance(candidates.get(prop_type, []), list)
    ) if isinstance(candidates, dict) else 0
    lines = [
        "🧪 BETGPTAI HIT PROPS DEBUG",
        "",
        f"Card Date: {payload.get('display_date') if isinstance(payload, dict) else 'Unavailable'}",
        "",
        f"Games scanned: {debug.get('games_scanned', 0)}",
        f"Total hitters scanned: {debug.get('total_hitters_scanned', debug.get('players_scanned', 0))}",
        f"Valid hitters: {debug.get('valid_hitters', 0)}",
        f"Rejected hitters: {debug.get('rejected_hitters', 0)}",
        "",
        "Reason counts:",
    ]
    if reason_counts:
        lines.extend(f"- {reason}: {count}" for reason, count in sorted(reason_counts.items()))
    else:
        lines.append("- None")
    lines.extend(["", "Top candidates before filtering:"])
    if top_before:
        for item in top_before[:10]:
            lines.append(
                f"- {item.get('player_name')} — {item.get('team_name')} — "
                f"{item.get('prop_type')} — score {item.get('raw_score')} — "
                f"grade {item.get('confidence_grade')}"
            )
    else:
        lines.append("- None")
    lines.extend([
        "",
        f"Final props created: {debug.get('final_props_created', final_count)}",
        f"Top 10 Hit Props: {len(candidates.get('hits', [])) if isinstance(candidates, dict) else 0}",
        f"Top 10 HR Props: {len(candidates.get('home_runs', [])) if isinstance(candidates, dict) else 0}",
        f"Top 10 RBI Props: {len(candidates.get('rbis', [])) if isinstance(candidates, dict) else 0}",
        f"Top 10 Total Bases Props: {len(candidates.get('total_bases', [])) if isinstance(candidates, dict) else 0}",
        f"Top 10 Strikeout Props: {len(candidates.get('strikeouts', [])) if isinstance(candidates, dict) else 0}",
    ])
    if int(debug.get("final_props_created") or final_count or 0) <= 0:
        lines.extend(["", "No qualified props available."])
    return "\n".join(lines).strip()


def render_props_test(payload: dict[str, Any]) -> str:
    """Render owner-only engine status output."""
    debug = payload.get("debug", {})
    sources = debug.get("data_sources_used", {})
    saved_count = len(payload.get("all_props", []))
    missing_count = len(debug.get("missing_fields", []) or [])
    status = "✅ Available" if saved_count else "⚠️ Available, no qualified props"
    return (
        "⚾ BETGPTAI PLAYER PROPS ENGINE STATUS\n\n"
        f"MLB Stats API: {'✅ Available' if sources.get('mlb_stats_api') else '❌ Unavailable'}\n"
        f"Baseball Savant: {'✅ Available' if sources.get('baseball_savant') else '❌ Unavailable'}\n"
        f"FanGraphs optional: {'✅ Available' if sources.get('fangraphs_optional') else '❌ Optional/Unavailable'}\n"
        f"Odds props optional: {'✅ Available' if sources.get('odds_props_optional') else '❌ Optional/Unavailable'}\n"
        f"Weather: {'✅ Available' if sources.get('weather') else '❌ Unavailable'}\n"
        f"Lineups: {'✅ Available' if sources.get('lineups') else '❌ Unavailable'}\n"
        f"Props generated: {debug.get('candidate_props_created', 0)}\n"
        f"Props saved: {saved_count}\n"
        f"Props missing data: {missing_count}\n"
        f"Engine Status: {status}"
    )


def grade_hit_prop(prop: dict[str, Any], game_data: dict[str, Any]) -> str:
    """Placeholder for future player prop grading."""
    del prop, game_data
    return "pending"


def grade_hr_prop(prop: dict[str, Any], game_data: dict[str, Any]) -> str:
    del prop, game_data
    return "pending"


def grade_strikeout_prop(prop: dict[str, Any], game_data: dict[str, Any]) -> str:
    del prop, game_data
    return "pending"


def grade_total_bases_prop(prop: dict[str, Any], game_data: dict[str, Any]) -> str:
    del prop, game_data
    return "pending"


def grade_pitcher_outs_prop(prop: dict[str, Any], game_data: dict[str, Any]) -> str:
    del prop, game_data
    return "pending"
