"""Admin-only MLB hitting streak report for BETGPTAI.

The report focuses on hitters batting 1-5 in today's lineups who have an
active 2+ game hitting streak. Confirmed lineups come from MLB Stats API
boxscores. If confirmed lineups are not posted yet, the report can use the
projected top-five hitter pool from the existing enriched MLB slate and labels
those entries as projected.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from hitting_streaks import get_hitting_streak
from mlb_data import get_combined_slate, get_mlb_schedule
from player_verification import verify_player_team_by_id
from storage import data_file


EASTERN = ZoneInfo("America/New_York")
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
REQUEST_TIMEOUT = 15


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _format_game_time_et(value: Any) -> str:
    """Format an MLB API timestamp into 12-hour Eastern Time."""
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


def _safe_name(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if "," in text:
        last, first = [part.strip() for part in text.split(",", 1)]
        if first and last:
            return f"{first} {last}"
    return text


def _player_id(row: dict[str, Any]) -> Any:
    return (
        row.get("player_id")
        or row.get("id")
        or row.get("batter")
        or row.get("mlbam_id")
    )


def _hits_pattern(values: list[Any], size: int = 5) -> str:
    """Render recent hit counts as H/0 style tokens."""
    tokens = []
    for value in values[:size]:
        try:
            hits = int(float(str(value)))
        except (TypeError, ValueError):
            hits = 0
        tokens.append("H" if hits > 0 else "0")
    return "-".join(tokens) if tokens else "Unavailable"


def _rate_short(rate: Any) -> str:
    """Convert '8/10 games' into '8/10' for compact report display."""
    text = str(rate or "Unavailable")
    return text.replace(" games", "")


def _fetch_boxscore(game_pk: Any) -> dict[str, Any]:
    response = requests.get(
        MLB_BOXSCORE_URL.format(game_pk=game_pk),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _confirmed_lineup_for_side(
    boxscore: dict[str, Any],
    side: str,
    team_name: str,
    opponent_name: str,
    game: dict[str, Any],
) -> list[dict[str, Any]]:
    """Extract batting-order spots 1-5 from MLB Stats API boxscore."""
    team_payload = ((boxscore.get("teams") or {}).get(side) or {})
    batting_order = team_payload.get("battingOrder") or []
    players = team_payload.get("players") or {}
    rows: list[dict[str, Any]] = []
    for index, raw_id in enumerate(batting_order[:5], start=1):
        person_payload = players.get(f"ID{raw_id}") or {}
        person = person_payload.get("person") or {}
        player_id = person.get("id") or raw_id
        name = _safe_name(person.get("fullName"))
        if not name:
            continue
        rows.append(
            {
                "player_id": player_id,
                "player_name": name,
                "team": team_name,
                "opponent": opponent_name,
                "batting_spot": index,
                "lineup_status": "confirmed",
                "game_pk": game.get("game_id") or game.get("game_pk"),
                "game_time": game.get("game_time"),
                "game_time_et": _format_game_time_et(game.get("game_time")),
                "away_team": game.get("away_team"),
                "home_team": game.get("home_team"),
            }
        )
    return rows


def _projected_lineup_for_side(
    slate_game: dict[str, Any],
    side: str,
    game: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build projected top-five candidates from Savant enrichment when needed."""
    savant = slate_game.get("savant") if isinstance(slate_game.get("savant"), dict) else {}
    batters = savant.get(f"{side}_batters") if isinstance(savant, dict) else []
    if not isinstance(batters, list):
        return []
    opponent = "home" if side == "away" else "away"
    team_name = game.get(f"{side}_team")
    opponent_name = game.get(f"{opponent}_team")
    rows: list[dict[str, Any]] = []
    for index, row in enumerate([item for item in batters if isinstance(item, dict)][:5], start=1):
        player_id = _player_id(row)
        name = _safe_name(row.get("player") or row.get("Name") or row.get("name"))
        if not name:
            continue
        rows.append(
            {
                "player_id": player_id,
                "player_name": name,
                "team": team_name,
                "opponent": opponent_name,
                "batting_spot": index,
                "lineup_status": "projected",
                "game_pk": game.get("game_id") or game.get("game_pk"),
                "game_time": game.get("game_time"),
                "game_time_et": _format_game_time_et(game.get("game_time")),
                "away_team": game.get("away_team"),
                "home_team": game.get("home_team"),
            }
        )
    return rows


def _slate_by_game_pk(slate: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(game.get("game_pk") or game.get("game_id")): game
        for game in slate
        if game.get("game_pk") or game.get("game_id")
    }


def _qualify_player(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Verify team and active streak, returning a qualified report row or reason."""
    player_id = row.get("player_id")
    verification = verify_player_team_by_id(player_id, str(row.get("team") or ""))
    if not verification.get("verified"):
        return None, (
            f"{row.get('player_name')} rejected: current team verification failed "
            f"({verification.get('reason')})"
        )

    current_team = verification.get("current_team") or row.get("team")
    profile = get_hitting_streak(
        player_id,
        str(verification.get("player_name") or row.get("player_name") or ""),
        str(current_team or ""),
    )
    if not profile.get("available"):
        return None, f"{row.get('player_name')} rejected: hitting game log unavailable"
    streak = int(profile.get("games_with_hit_streak") or 0)
    if streak < 2:
        return None, f"{row.get('player_name')} rejected: hit streak below 2 games"

    qualified = {
        **row,
        "player_name": verification.get("player_name") or row.get("player_name"),
        "team": current_team,
        "current_team": current_team,
        "games_with_hit_streak": streak,
        "current_hit_streak": f"{streak} games" if streak != 1 else "1 game",
        "last_5_games": _hits_pattern(profile.get("last_5_hits") or []),
        "last_10_hit_rate": _rate_short(profile.get("hit_rate_last_10")),
        "last_15_hit_rate": _rate_short(profile.get("hit_rate_last_15")),
        "multi_hit_games_last_10": profile.get("multi_hit_games_last_10", 0),
        "last_game_with_hit": profile.get("last_game_with_hit"),
        "active_streak": True,
    }
    return qualified, None


def _save_report(card_date: str, payload: dict[str, Any]) -> Path:
    path = data_file(f"hitting_streak_report_{card_date}.json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def build_hitting_streak_report(
    card_date: str,
    *,
    odds_api_key: str = "",
    highlightly_api_key: str = "",
) -> dict[str, Any]:
    """Build and save the admin-only daily hitting streak report."""
    schedule = get_mlb_schedule(card_date)
    try:
        slate = get_combined_slate(
            odds_api_key,
            game_date=card_date,
            highlightly_api_key=highlightly_api_key,
        )
    except Exception:
        slate = []
    slate_index = _slate_by_game_pk(slate)

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rejected: list[str] = []
    games_scanned = 0
    lineups_found = 0
    confirmed_lineups = 0
    projected_lineups = 0
    players_scanned = 0

    for game in schedule:
        game_pk = game.get("game_id") or game.get("game_pk")
        if not game_pk:
            rejected.append(f"{game.get('away_team')} @ {game.get('home_team')}: missing game_pk")
            continue
        games_scanned += 1
        game_key = (
            f"{game.get('away_team')} vs {game.get('home_team')} — "
            f"{_format_game_time_et(game.get('game_time'))}"
        )

        candidate_rows: list[dict[str, Any]] = []
        try:
            boxscore = _fetch_boxscore(game_pk)
            away_rows = _confirmed_lineup_for_side(
                boxscore, "away", str(game.get("away_team")), str(game.get("home_team")), game
            )
            home_rows = _confirmed_lineup_for_side(
                boxscore, "home", str(game.get("home_team")), str(game.get("away_team")), game
            )
            candidate_rows = away_rows + home_rows
            confirmed_sides = int(bool(away_rows)) + int(bool(home_rows))
            if confirmed_sides:
                lineups_found += confirmed_sides
                confirmed_lineups += confirmed_sides
        except Exception as error:
            rejected.append(f"{game_key}: confirmed lineup unavailable ({error})")

        if not candidate_rows:
            slate_game = slate_index.get(str(game_pk), {})
            away_rows = _projected_lineup_for_side(slate_game, "away", game)
            home_rows = _projected_lineup_for_side(slate_game, "home", game)
            candidate_rows = away_rows + home_rows
            projected_sides = int(bool(away_rows)) + int(bool(home_rows))
            if projected_sides:
                lineups_found += projected_sides
                projected_lineups += projected_sides

        players_scanned += len(candidate_rows)
        for row in candidate_rows:
            if int(row.get("batting_spot") or 99) > 5:
                rejected.append(f"{row.get('player_name')} rejected: batting spot outside 1-5")
                continue
            qualified, reason = _qualify_player(row)
            if qualified:
                grouped[game_key].append(qualified)
            elif reason:
                rejected.append(reason)

    for players in grouped.values():
        players.sort(
            key=lambda item: (
                int(item.get("games_with_hit_streak") or 0),
                int(str(item.get("last_10_hit_rate") or "0").split("/", 1)[0] or 0),
            ),
            reverse=True,
        )

    qualified_count = sum(len(players) for players in grouped.values())
    payload = {
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "games": dict(grouped),
        "debug": {
            "games_scanned": games_scanned,
            "lineups_found": lineups_found,
            "confirmed_lineups_count": confirmed_lineups,
            "projected_lineups_count": projected_lineups,
            "players_scanned": players_scanned,
            "qualified_players": qualified_count,
            "players_rejected": rejected[:200],
        },
    }
    payload["report_path"] = str(_save_report(card_date, payload))
    return payload


def render_hitting_streak_report(payload: dict[str, Any]) -> str:
    """Render the admin-only report in Telegram-friendly text."""
    lines = [
        "🔥 BETGPTAI HIT STREAK REPORT",
        f"📅 Date: {payload.get('display_date')}",
        "🧪 Admin Only",
        "",
    ]
    games = payload.get("games") if isinstance(payload.get("games"), dict) else {}
    if not games:
        lines.append("No 1–5 lineup hitters with 2+ game hit streak found yet.")
        return "\n".join(lines).strip()

    for game_label, players in games.items():
        lines.extend([game_label, ""])
        for index, player in enumerate(players, start=1):
            lines.extend(
                [
                    f"{index}. {player.get('player_name')}",
                    f"Team: {player.get('team')}",
                    f"Opponent: {player.get('opponent')}",
                    f"Batting Spot: {player.get('batting_spot')}",
                    f"Hit Streak: {player.get('current_hit_streak')}",
                    f"Last 5: {player.get('last_5_games')}",
                    f"Last 10 Hit Rate: {player.get('last_10_hit_rate')}",
                    f"Game Time ET: {player.get('game_time_et')}",
                    f"Lineup Status: {player.get('lineup_status')}",
                    "",
                ]
            )
        lines.append("━━━━━━━━━━━━")
        lines.append("")
    lines.append("Admin research report only. No automatic bet recommendation.")
    return "\n".join(lines).strip()


def render_hitting_streak_debug(payload: dict[str, Any]) -> str:
    """Render owner-only debug details for the streak report."""
    debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}
    rejected = debug.get("players_rejected") or []
    lines = [
        "🧪 BETGPTAI STREAK DEBUG",
        "",
        f"Date: {payload.get('display_date')}",
        f"Games scanned: {debug.get('games_scanned', 0)}",
        f"Lineups found: {debug.get('lineups_found', 0)}",
        f"Confirmed lineups count: {debug.get('confirmed_lineups_count', 0)}",
        f"Projected lineups count: {debug.get('projected_lineups_count', 0)}",
        f"Players scanned: {debug.get('players_scanned', 0)}",
        f"Qualified players: {debug.get('qualified_players', 0)}",
        "",
        "Players rejected and why:",
    ]
    lines.extend(f"- {item}" for item in rejected[:50])
    if not rejected:
        lines.append("- None")
    lines.extend(["", f"Saved report: {payload.get('report_path')}"])
    return "\n".join(lines).strip()
