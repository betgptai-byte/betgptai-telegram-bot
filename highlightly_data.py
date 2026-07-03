"""Optional Highlightly enrichment for MLB games.

Highlightly plan coverage varies. Every helper raises a friendly exception, and
the merge layer turns unavailable endpoints into the literal string
``unavailable`` instead of interrupting the rest of the pipeline.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any

import requests


HIGHLIGHTLY_BASE_URL = "https://sports.highlightly.net"
BASEBALL_PATH = "/baseball"
REQUEST_TIMEOUT = 20
UNAVAILABLE = "unavailable"
CACHE_TTL = timedelta(hours=6)
_CACHE: dict[tuple[str, str], tuple[datetime, list[dict[str, Any]]]] = {}
_THROTTLED_UNTIL: datetime | None = None


class HighlightlyDataError(Exception):
    """Raised when Highlightly cannot provide an endpoint response."""


def _get_json(api_key: str, path: str, params: dict[str, Any] | None = None) -> Any:
    """Make one authenticated request without ever putting the key in the URL."""
    if not api_key:
        raise HighlightlyDataError("HIGHLIGHTLY_API_KEY is missing.")
    try:
        response = requests.get(
            f"{HIGHLIGHTLY_BASE_URL}{BASEBALL_PATH}{path}",
            params=params or {},
            headers={"x-rapidapi-key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 429:
            raise HighlightlyDataError("Highlightly rate limited this key.")
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        raise HighlightlyDataError(f"Highlightly returned HTTP status {status}.") from error
    except requests.RequestException as error:
        raise HighlightlyDataError("Could not connect to Highlightly.") from error
    except ValueError as error:
        raise HighlightlyDataError("Highlightly returned invalid JSON.") from error


def _records(payload: Any) -> list[dict[str, Any]]:
    """Normalize plain arrays and paginated {data: [...]} responses."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return [payload]
    return []


def _normalize_team(name: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return {
        "oaklandathletics": "athletics",
        "sacramentoathletics": "athletics",
        "athletics": "athletics",
        "arizonadbacks": "arizonadiamondbacks",
    }.get(normalized, normalized)


def _team_name(team: dict[str, Any]) -> str:
    return str(team.get("displayName") or team.get("name") or "")


def _game_key(away_team: str, home_team: str) -> frozenset[str]:
    return frozenset((_normalize_team(away_team), _normalize_team(home_team)))


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _closest_match(game: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve doubleheaders by choosing the nearest scheduled start time."""
    if not matches:
        return None
    game_time = _parse_time(game.get("game_time"))
    if game_time is None:
        return matches.pop(0)

    def distance(match: dict[str, Any]) -> float:
        match_time = _parse_time(match.get("date"))
        return abs((game_time - match_time).total_seconds()) if match_time else float("inf")

    match = min(matches, key=distance)
    matches.remove(match)
    return match


# These helpers correspond to the baseball endpoints commonly present on the
# free plan. If the account cannot access one, the caller receives unavailable.
def get_daily_matches(api_key: str, game_date: str) -> list[dict[str, Any]]:
    payload = _get_json(api_key, "/matches", {
        "league": "MLB", "date": game_date, "timezone": "America/New_York", "limit": 100,
    })
    return _records(payload)


def get_match_info(api_key: str, match_id: int) -> dict[str, Any] | str:
    records = _records(_get_json(api_key, f"/matches/{match_id}"))
    return records[0] if records else UNAVAILABLE


def get_starting_lineups(api_key: str, match_id: int) -> Any:
    payload = _get_json(api_key, f"/lineups/{match_id}")
    return payload if payload else UNAVAILABLE


def get_team_statistics(api_key: str, team_id: int, season_start: str) -> Any:
    records = _records(_get_json(
        api_key, f"/teams/statistics/{team_id}",
        {"fromDate": season_start, "timezone": "America/New_York"},
    ))
    return records[0] if records else UNAVAILABLE


def get_recent_matches(api_key: str, team_id: int) -> Any:
    records = _records(_get_json(api_key, "/last-five-games", {"teamId": team_id}))
    return records[:5] if records else UNAVAILABLE


def get_injuries_from_match_info(match_info: Any) -> Any:
    """Read injuries included by the match-info endpoint, when covered."""
    return _compact(match_info.get("injuries")) if isinstance(match_info, dict) else UNAVAILABLE


def get_news_from_match_info(match_info: Any) -> Any:
    """Read news/articles included by match info; there is no assumed endpoint."""
    if not isinstance(match_info, dict):
        return UNAVAILABLE
    return _compact(match_info.get("news") or match_info.get("articles"))


def _runs(score: Any) -> int | float | None:
    """Extract a baseball run total from Highlightly's varying score shapes."""
    if isinstance(score, (int, float)):
        return score
    if not isinstance(score, dict):
        return None
    innings = score.get("innings")
    if isinstance(innings, list):
        return sum(value for value in innings if isinstance(value, (int, float)))
    for key in ("runs", "total", "current"):
        if isinstance(score.get(key), (int, float)):
            return score[key]
    return None


def summarize_team_form(recent_matches: Any, team_id: int) -> Any:
    """Turn recent matches into a compact W-L and runs summary when possible."""
    if not isinstance(recent_matches, list) or not recent_matches:
        return UNAVAILABLE
    wins = losses = runs_scored = runs_allowed = 0
    usable = 0
    for game in recent_matches[:5]:
        away, home = game.get("awayTeam", {}), game.get("homeTeam", {})
        score = game.get("state", {}).get("score", {})
        away_runs, home_runs = _runs(score.get("away")), _runs(score.get("home"))
        if away_runs is None or home_runs is None:
            continue
        is_home = home.get("id") == team_id
        team_runs, opponent_runs = (home_runs, away_runs) if is_home else (away_runs, home_runs)
        wins += int(team_runs > opponent_runs)
        losses += int(team_runs < opponent_runs)
        runs_scored += team_runs
        runs_allowed += opponent_runs
        usable += 1
    if not usable:
        return UNAVAILABLE
    return {"games": usable, "wins": wins, "losses": losses,
            "runs_scored": runs_scored, "runs_allowed": runs_allowed}


def _compact(value: Any, limit: int = 8) -> Any:
    if isinstance(value, dict) and isinstance(value.get("data"), list):
        value = value["data"]
    if isinstance(value, list):
        return value[:limit] or UNAVAILABLE
    return value if value else UNAVAILABLE


def _unavailable_context() -> dict[str, Any]:
    return {
        "recent_matches": {"away": UNAVAILABLE, "home": UNAVAILABLE},
        "injuries": UNAVAILABLE,
        "news": UNAVAILABLE,
        "lineups": UNAVAILABLE,
        "team_form": {"away": UNAVAILABLE, "home": UNAVAILABLE},
        "match_info": UNAVAILABLE,
        "match_preview": UNAVAILABLE,
        "team_statistics": {"away": UNAVAILABLE, "home": UNAVAILABLE},
    }


def merge_highlightly_data(
    slate: list[dict[str, Any]], api_key: str, game_date: str
) -> list[dict[str, Any]]:
    """Merge whatever the user's Highlightly plan makes available."""
    global _THROTTLED_UNTIL
    for game in slate:
        game["highlightly"] = _unavailable_context()
    if not api_key:
        return slate
    now = datetime.utcnow()
    if _THROTTLED_UNTIL and now < _THROTTLED_UNTIL:
        return slate

    cache_key = (api_key[-8:], game_date)
    cached = _CACHE.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        matches = get_daily_matches(api_key, game_date)
    except HighlightlyDataError as error:
        if "rate limited" in str(error).lower() or "429" in str(error):
            _THROTTLED_UNTIL = now + CACHE_TTL
            return slate
        raise
    matches_by_game: dict[frozenset[str], list[dict[str, Any]]] = {}
    for match in matches:
        key = _game_key(_team_name(match.get("awayTeam", {})), _team_name(match.get("homeTeam", {})))
        matches_by_game.setdefault(key, []).append(match)

    matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for game in slate:
        key = _game_key(game["away_team"], game["home_team"])
        match = _closest_match(game, matches_by_game.get(key, []))
        if match and isinstance(match.get("id"), int):
            game["highlightly_match_id"] = match["id"]
            matched.append((game, match))

    team_ids = {
        team.get("id") for _, match in matched
        for team in (match.get("awayTeam", {}), match.get("homeTeam", {}))
        if isinstance(team.get("id"), int)
    }
    details: dict[int, Any] = {}
    lineups: dict[int, Any] = {}
    recent: dict[int, Any] = {}
    stats: dict[int, Any] = {}
    season_start = f"{game_date[:4]}-03-01"

    # Each endpoint is independent. A 403 or plan restriction affects only that field.
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures: dict[Any, tuple[str, int]] = {}
        for game, _ in matched:
            match_id = game["highlightly_match_id"]
            futures[executor.submit(get_match_info, api_key, match_id)] = ("detail", match_id)
            futures[executor.submit(get_starting_lineups, api_key, match_id)] = ("lineup", match_id)
        for team_id in team_ids:
            futures[executor.submit(get_recent_matches, api_key, team_id)] = ("recent", team_id)
            futures[executor.submit(get_team_statistics, api_key, team_id, season_start)] = ("stats", team_id)
        for future in as_completed(futures):
            kind, identifier = futures[future]
            try:
                result = future.result()
            except Exception:
                # Endpoint restrictions/rate limits are optional enrichment.
                result = UNAVAILABLE
            {"detail": details, "lineup": lineups, "recent": recent, "stats": stats}[kind][identifier] = result

    for game, match in matched:
        match_id = game["highlightly_match_id"]
        detail = details.get(match_id, UNAVAILABLE)
        away_id, home_id = match.get("awayTeam", {}).get("id"), match.get("homeTeam", {}).get("id")
        away_recent, home_recent = recent.get(away_id, UNAVAILABLE), recent.get(home_id, UNAVAILABLE)
        context = game["highlightly"]
        context["recent_matches"] = {"away": away_recent, "home": home_recent}
        context["team_form"] = {
            "away": summarize_team_form(away_recent, away_id),
            "home": summarize_team_form(home_recent, home_id),
        }
        context["lineups"] = _compact(lineups.get(match_id, UNAVAILABLE))
        context["team_statistics"] = {
            "away": stats.get(away_id, UNAVAILABLE), "home": stats.get(home_id, UNAVAILABLE),
        }
        if isinstance(detail, dict):
            context["match_info"] = detail
            context["injuries"] = get_injuries_from_match_info(detail)
            context["news"] = get_news_from_match_info(detail)
            context["match_preview"] = _compact(detail.get("preview") or detail.get("predictions"))
    _CACHE[cache_key] = (now, slate)
    return slate
