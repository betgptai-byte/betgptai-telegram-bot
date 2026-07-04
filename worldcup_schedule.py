"""World Cup schedule fallback for BETGPTAI soccer cards.

This fallback is intentionally small and explicit. It is only used when
WORLD_CUP_MODE=true, and it prevents /soccer from returning an empty card when
the regular free/optional providers do not expose current tournament fixtures.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


WORLD_CUP_FALLBACK: dict[str, list[dict[str, Any]]] = {
    "2026-07-03": [
        {
            "match_id": "wc_2026_0703_australia_egypt",
            "home_team": "Australia",
            "away_team": "Egypt",
            "game_time_et": "2:00 PM ET",
            "hour": 14,
            "minute": 0,
            "round": "World Cup",
            "status": "TIMED",
        },
        {
            "match_id": "wc_2026_0703_argentina_cape_verde",
            "home_team": "Argentina",
            "away_team": "Cape Verde",
            "game_time_et": "6:00 PM ET",
            "hour": 18,
            "minute": 0,
            "round": "World Cup",
            "status": "TIMED",
        },
        {
            "match_id": "wc_2026_0703_colombia_ghana",
            "home_team": "Colombia",
            "away_team": "Ghana",
            "game_time_et": "9:30 PM ET",
            "hour": 21,
            "minute": 30,
            "round": "World Cup",
            "status": "TIMED",
        },
    ],
}


def world_cup_mode_enabled() -> bool:
    """Return whether the explicit World Cup fallback should be active."""
    return os.getenv("WORLD_CUP_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _iso_kickoff(game_date: str, hour: int, minute: int) -> str:
    """Create an ISO kickoff timestamp with Eastern Time offset."""
    kickoff = datetime.combine(
        date.fromisoformat(game_date),
        time(hour=hour, minute=minute),
        tzinfo=EASTERN,
    )
    return kickoff.isoformat()


def _normalize_match(game_date: str, row: dict[str, Any]) -> dict[str, Any]:
    """Return a soccer-slate-compatible World Cup fallback match."""
    return {
        "match_id": row["match_id"],
        "id": row["match_id"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "game_time_et": row["game_time_et"],
        "kickoff": _iso_kickoff(game_date, int(row["hour"]), int(row["minute"])),
        "utcDate": _iso_kickoff(game_date, int(row["hour"]), int(row["minute"])),
        "round": row.get("round", "World Cup"),
        "stage": row.get("round", "World Cup"),
        "status": row.get("status", "TIMED"),
        "competition": "FIFA World Cup",
        "competition_code": "WC",
        "area_name": "International",
        "area_code": "INT",
        "score": {"fullTime": {"home": None, "away": None}},
        "homeTeam": {"id": None, "name": row["home_team"]},
        "awayTeam": {"id": None, "name": row["away_team"]},
        "world_cup_context": {
            "source": "world_cup_fallback",
            "round": row.get("round", "World Cup"),
            "game_time_et": row["game_time_et"],
        },
        "home_recent": "unavailable",
        "away_recent": "unavailable",
        "league_environment": "unavailable",
        "h2h_history": "unavailable",
        "motivation_context": {"stage": row.get("round", "World Cup")},
        "corners_profile": "unavailable",
        "weather": "unavailable",
        "best_available_prices": [],
        "odds_status": "unavailable",
    }


def get_world_cup_fallback_matches(game_date: str | None = None) -> list[dict[str, Any]]:
    """Return fallback matches for today/tomorrow when World Cup mode is active."""
    if not world_cup_mode_enabled():
        return []
    base_day = date.fromisoformat(game_date) if game_date else datetime.now(EASTERN).date()
    matches: list[dict[str, Any]] = []
    for current_day in (base_day, base_day + timedelta(days=1)):
        date_text = current_day.isoformat()
        for row in WORLD_CUP_FALLBACK.get(date_text, []):
            matches.append(_normalize_match(date_text, row))
    return matches
