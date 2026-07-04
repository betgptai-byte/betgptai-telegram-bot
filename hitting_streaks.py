"""MLB hitting-streak helper for admin-only BETGPTAI prop analysis.

This module uses MLB Stats API game logs as the primary source. It is designed
to be optional and non-blocking: if game logs are unavailable, player props
continue without streak boosts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests


EASTERN = ZoneInfo("America/New_York")
MLB_PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
REQUEST_TIMEOUT = 10
_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def _current_season() -> int:
    """Return the current ET calendar year for MLB Stats API season requests."""
    return datetime.now(EASTERN).year


def _int(value: Any, default: int = 0) -> int:
    """Safely convert MLB stat values into integers."""
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _game_date(split: dict[str, Any]) -> str:
    """Extract a sortable game date from one MLB Stats API game-log split."""
    date_value = (
        split.get("date")
        or (split.get("game") or {}).get("gameDate")
        or (split.get("game") or {}).get("officialDate")
        or ""
    )
    return str(date_value)[:10]


def _fetch_game_log(player_id: int, season: int) -> list[dict[str, Any]]:
    """Fetch a hitter's season game log from MLB Stats API."""
    response = requests.get(
        f"{MLB_PEOPLE_URL}/{player_id}/stats",
        params={"stats": "gameLog", "group": "hitting", "season": season},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    stats = payload.get("stats") or []
    if not stats:
        return []
    splits = stats[0].get("splits") or []
    return [split for split in splits if isinstance(split, dict)]


def get_hitting_streak(
    player_id: Any,
    player_name: str = "",
    current_team: str = "",
    *,
    season: int | None = None,
) -> dict[str, Any]:
    """Return a compact hitting-streak profile for one MLB hitter.

    Returned fields:
    - player_id
    - player_name
    - current_team
    - games_with_hit_streak
    - last_5_hits
    - last_10_hits
    - last_15_hits
    - hit_rate_last_10
    - hit_rate_last_15
    - multi_hit_games_last_10
    - last_game_with_hit
    - active_streak
    """
    try:
        numeric_id = int(str(player_id))
    except (TypeError, ValueError):
        return {
            "player_id": player_id,
            "player_name": player_name,
            "current_team": current_team,
            "games_with_hit_streak": 0,
            "last_5_hits": [],
            "last_10_hits": [],
            "last_15_hits": [],
            "hit_rate_last_10": "0/0 games",
            "hit_rate_last_15": "0/0 games",
            "multi_hit_games_last_10": 0,
            "last_game_with_hit": None,
            "active_streak": False,
            "available": False,
            "reason": "No MLB player ID available for hitting-streak lookup.",
        }

    selected_season = season or _current_season()
    cache_key = (str(numeric_id), selected_season)
    if cache_key in _CACHE:
        cached = dict(_CACHE[cache_key])
        cached["player_name"] = cached.get("player_name") or player_name
        cached["current_team"] = cached.get("current_team") or current_team
        return cached

    try:
        splits = _fetch_game_log(numeric_id, selected_season)
    except Exception as error:
        return {
            "player_id": numeric_id,
            "player_name": player_name,
            "current_team": current_team,
            "games_with_hit_streak": 0,
            "last_5_hits": [],
            "last_10_hits": [],
            "last_15_hits": [],
            "hit_rate_last_10": "0/0 games",
            "hit_rate_last_15": "0/0 games",
            "multi_hit_games_last_10": 0,
            "last_game_with_hit": None,
            "active_streak": False,
            "available": False,
            "reason": f"MLB game-log lookup unavailable: {error}",
        }

    games = sorted(
        splits,
        key=lambda split: _game_date(split),
        reverse=True,
    )
    hit_counts = [_int((game.get("stat") or {}).get("hits")) for game in games[:15]]
    last_5 = hit_counts[:5]
    last_10 = hit_counts[:10]
    last_15 = hit_counts[:15]

    active_games = 0
    for hits in hit_counts:
        if hits > 0:
            active_games += 1
        else:
            break

    hit_games_10 = sum(1 for hits in last_10 if hits > 0)
    hit_games_15 = sum(1 for hits in last_15 if hits > 0)
    multi_hit_10 = sum(1 for hits in last_10 if hits >= 2)
    last_hit_date = None
    for game, hits in zip(games[:15], hit_counts):
        if hits > 0:
            last_hit_date = _game_date(game) or None
            break

    result = {
        "player_id": numeric_id,
        "player_name": player_name,
        "current_team": current_team,
        "games_with_hit_streak": active_games,
        "last_5_hits": last_5,
        "last_10_hits": last_10,
        "last_15_hits": last_15,
        "hit_rate_last_10": f"{hit_games_10}/{len(last_10)} games",
        "hit_rate_last_15": f"{hit_games_15}/{len(last_15)} games",
        "hit_games_last_10": hit_games_10,
        "hit_games_last_15": hit_games_15,
        "multi_hit_games_last_10": multi_hit_10,
        "last_game_with_hit": last_hit_date,
        "active_streak": active_games > 0,
        "available": bool(games),
        "reason": "Hitting-streak profile available." if games else "No game-log splits found.",
    }
    _CACHE[cache_key] = dict(result)
    return result


def hitting_streak_score_adjustment(profile: dict[str, Any]) -> float:
    """Return the internal score boost/downgrade for a hit-prop candidate."""
    if not profile.get("available"):
        return 0.0
    streak = _int(profile.get("games_with_hit_streak"))
    hit_games_10 = _int(profile.get("hit_games_last_10"))
    multi_hit_10 = _int(profile.get("multi_hit_games_last_10"))
    last_5 = profile.get("last_5_hits") if isinstance(profile.get("last_5_hits"), list) else []

    adjustment = 0.0
    if streak >= 8:
        adjustment += 8.0
    elif streak >= 5:
        adjustment += 5.0
    elif streak >= 3:
        adjustment += 2.5

    if hit_games_10 >= 8:
        adjustment += 4.0
    elif hit_games_10 >= 7:
        adjustment += 2.5

    if multi_hit_10 >= 3:
        adjustment += 2.0

    if len(last_5) >= 3 and all(_int(hits) == 0 for hits in last_5[:3]):
        adjustment -= 7.0

    return adjustment
