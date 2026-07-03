"""Optional API-Sports Baseball fallback/enrichment for BETGPTAI.

This module is intentionally backup-only. MLB Stats API and Baseball Savant
remain the primary baseball sources. API-Sports Baseball is used only when it
can add context safely, or when the MLB schedule source temporarily fails.

API key notes:
- If API-Sports uses the same account key as API-Football, leave
  API_SPORTS_KEY blank and the bot can reuse API_FOOTBALL_KEY.
- If Baseball has its own key, set API_SPORTS_KEY in .env.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Any

import requests


BASE_URL = "https://v1.baseball.api-sports.io"
REQUEST_TIMEOUT = 20
UNAVAILABLE = "unavailable"


class APISportsBaseballError(Exception):
    """Raised when API-Sports Baseball cannot provide optional data."""


def get_api_sports_baseball_key() -> str:
    """Prefer API_SPORTS_KEY, then fall back to the API_FOOTBALL_KEY."""
    return (
        os.getenv("API_SPORTS_KEY", "").strip()
        or os.getenv("API_FOOTBALL_KEY", "").strip()
    )


def _get_json(
    endpoint: str,
    params: dict[str, Any] | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Call API-Sports Baseball while keeping secret keys out of errors."""
    key = (api_key or get_api_sports_baseball_key()).strip()
    if not key:
        raise APISportsBaseballError("API_SPORTS_KEY/API_FOOTBALL_KEY is missing.")
    try:
        response = requests.get(
            f"{BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}",
            params=params or {},
            headers={"x-apisports-key": key},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise APISportsBaseballError(
            "API-Sports Baseball is temporarily unavailable."
        ) from error
    if not isinstance(payload, dict):
        raise APISportsBaseballError("API-Sports Baseball returned unexpected data.")
    errors = payload.get("errors")
    if errors:
        logging.warning("API-Sports Baseball returned errors: %s", errors)
    return payload


def _response_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Safely read API-Sports response arrays."""
    response = payload.get("response", [])
    return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []


def api_sports_baseball_available(api_key: str | None = None) -> bool:
    """Lightweight owner-only status check for API-Sports Baseball."""
    try:
        payload = _get_json("status", api_key=api_key)
        return isinstance(payload.get("response"), dict) or "results" in payload
    except Exception:
        return False


def _normalize_team(name: str) -> str:
    """Normalize team names so backup games can be matched to MLB Stats games."""
    normalized = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return {
        "athletics": "athletics",
        "oaklandathletics": "athletics",
        "sacramentoathletics": "athletics",
        "arizonadbacks": "arizonadiamondbacks",
        "dbacks": "arizonadiamondbacks",
        "chisox": "chicagowhitesox",
    }.get(normalized, normalized)


def _game_key(away_team: str, home_team: str) -> frozenset[str]:
    """Use both teams as a loose game key because source ordering can vary."""
    return frozenset((_normalize_team(away_team), _normalize_team(home_team)))


def _team_name(container: dict[str, Any], side: str) -> str:
    team = container.get(side, {}) if isinstance(container.get(side), dict) else {}
    return str(team.get("name") or "Unknown")


def _team_id(container: dict[str, Any], side: str) -> Any:
    team = container.get(side, {}) if isinstance(container.get(side), dict) else {}
    return team.get("id")


def _score_total(scores: dict[str, Any], side: str) -> int | None:
    score = scores.get(side, {}) if isinstance(scores.get(side), dict) else {}
    total = score.get("total")
    return total if isinstance(total, int) else None


def _iso_game_time(raw: dict[str, Any]) -> str:
    """Convert API-Sports timestamps to ISO strings when available."""
    timestamp = raw.get("timestamp")
    if isinstance(timestamp, int):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    return str(raw.get("date") or "Unknown")


def _normalize_game(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Convert API-Sports Baseball games into the bot's MLB slate shape."""
    teams = raw.get("teams", {}) if isinstance(raw.get("teams"), dict) else {}
    scores = raw.get("scores", {}) if isinstance(raw.get("scores"), dict) else {}
    league = raw.get("league", {}) if isinstance(raw.get("league"), dict) else {}
    status = raw.get("status", {}) if isinstance(raw.get("status"), dict) else {}
    away_team = _team_name(teams, "away")
    home_team = _team_name(teams, "home")
    if away_team == "Unknown" or home_team == "Unknown":
        return None
    game_id = raw.get("id")
    away_score = _score_total(scores, "away")
    home_score = _score_total(scores, "home")
    return {
        "game_id": f"apisports_{game_id}" if game_id is not None else None,
        "game_time": _iso_game_time(raw),
        "status": status.get("long") or status.get("short") or "Unknown",
        "away_team": away_team,
        "home_team": home_team,
        "away_team_id": None,
        "home_team_id": None,
        "away_pitcher": "TBD",
        "home_pitcher": "TBD",
        "away_pitcher_id": None,
        "home_pitcher_id": None,
        "venue": raw.get("venue"),
        "away_score": away_score,
        "home_score": home_score,
        "schedule_source": "api_sports_baseball_backup",
        "api_sports_baseball_context": {
            "game_id": game_id,
            "league": league,
            "status": status,
            "scores": scores,
            "away_team_id": _team_id(teams, "away"),
            "home_team_id": _team_id(teams, "home"),
        },
    }


def get_api_sports_baseball_schedule(
    game_date: str | None = None,
    api_key: str | None = None,
    league_id: int | None = 1,
) -> list[dict[str, Any]]:
    """Fetch a one-day backup baseball schedule.

    API-Sports commonly uses league 1 for MLB. Pass league_id=None only when an
    owner tool intentionally wants international baseball events.
    """
    selected_date = game_date or date.today().isoformat()
    season = int(selected_date[:4])
    attempts = (
        [{"date": selected_date, "league": league_id, "season": season}]
        if league_id is not None
        else [{"date": selected_date}]
    )
    for params in attempts:
        try:
            payload = _get_json("games", params, api_key)
            games = [_normalize_game(item) for item in _response_list(payload)]
            normalized = [game for game in games if game is not None]
            if normalized:
                return normalized
        except Exception:
            logging.warning("API-Sports Baseball schedule lookup failed", exc_info=True)
    return []


def get_api_sports_baseball_standings(
    season: int,
    league_id: int = 1,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Optional standings backup for owner reports and model enrichment."""
    try:
        return _response_list(_get_json(
            "standings", {"league": league_id, "season": season}, api_key
        ))
    except Exception:
        logging.warning("API-Sports Baseball standings unavailable", exc_info=True)
        return []


def get_api_sports_team_stats(
    team_id: Any,
    season: int,
    league_id: int = 1,
    api_key: str | None = None,
) -> dict[str, Any] | str:
    """Optional team statistics backup when a mapped API-Sports team ID exists."""
    if not team_id:
        return UNAVAILABLE
    try:
        payload = _get_json(
            "teams/statistics",
            {"team": team_id, "league": league_id, "season": season},
            api_key,
        )
        response = payload.get("response")
        return response if response else UNAVAILABLE
    except Exception:
        logging.warning("API-Sports Baseball team stats unavailable", exc_info=True)
        return UNAVAILABLE


def get_api_sports_player_stats(
    player_id: Any,
    season: int,
    league_id: int = 1,
    api_key: str | None = None,
) -> dict[str, Any] | str:
    """Optional player statistics backup when a mapped API-Sports player ID exists."""
    if not player_id:
        return UNAVAILABLE
    try:
        payload = _get_json(
            "players/statistics",
            {"player": player_id, "league": league_id, "season": season},
            api_key,
        )
        response = payload.get("response")
        return response if response else UNAVAILABLE
    except Exception:
        logging.warning("API-Sports Baseball player stats unavailable", exc_info=True)
        return UNAVAILABLE


def get_api_sports_baseball_leagues(
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """List baseball leagues, including international leagues, when available."""
    try:
        return _response_list(_get_json("leagues", api_key=api_key))
    except Exception:
        logging.warning("API-Sports Baseball leagues unavailable", exc_info=True)
        return []


def merge_api_sports_baseball_data(
    slate: list[dict[str, Any]],
    selected_date: str,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Attach API-Sports Baseball context without changing the primary slate.

    This enriches existing MLB Stats games only when team names match. If the
    optional source is missing or fails, the original slate is returned.
    """
    if not slate:
        return slate
    try:
        backup_games = get_api_sports_baseball_schedule(selected_date, api_key)
    except Exception:
        logging.warning("API-Sports Baseball enrichment failed", exc_info=True)
        return slate
    by_key = {
        _game_key(game.get("away_team", ""), game.get("home_team", "")): game
        for game in backup_games
    }
    season = int(selected_date[:4])
    standings = get_api_sports_baseball_standings(season, api_key=api_key)
    for game in slate:
        match = by_key.get(_game_key(game.get("away_team", ""), game.get("home_team", "")))
        if not match:
            game.setdefault("api_sports_baseball_context", UNAVAILABLE)
            continue
        context = dict(match.get("api_sports_baseball_context") or {})
        context["standings_backup"] = standings or UNAVAILABLE
        # Team stat backup is best-effort and only attempted when API-Sports
        # team IDs are present in the matched backup game.
        context["away_team_stats_backup"] = get_api_sports_team_stats(
            context.get("away_team_id"), season, api_key=api_key
        )
        context["home_team_stats_backup"] = get_api_sports_team_stats(
            context.get("home_team_id"), season, api_key=api_key
        )
        game["api_sports_baseball_context"] = context
    return slate
