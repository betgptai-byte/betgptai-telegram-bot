"""Build a beginner-friendly soccer slate from free and optional data feeds."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests

from api_football import (
    APIFootballError,
    api_football_available,
    get_api_football_schedule,
    get_api_football_slate,
)
from fbref_data import fbref_available, merge_fbref_data
from game_time import game_sort_key
from soccer_master_engines import enrich_soccer_master_system
from statsbomb_data import merge_statsbomb_data, statsbomb_available
from thesportsdb_data import (
    check_thesportsdb_connection,
    get_soccer_events,
    thesportsdb_enabled,
)
from worldcup_schedule import get_world_cup_fallback_matches, world_cup_mode_enabled


FOOTBALL_DATA_URL = "https://api.football-data.org/v4/matches"
SPORTS_DB_BASE_URL = "https://www.thesportsdb.com/api/v1/json"
ODDS_BASE_URL = "https://api.the-odds-api.com/v4"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
CLUBELO_BASE_URL = "https://api.clubelo.com"
SERPAPI_URL = "https://serpapi.com/search.json"
REQUEST_TIMEOUT = 20
UNAVAILABLE = "unavailable"
LAST_SOCCER_DEBUG: dict[str, Any] = {}

COMPETITION_TO_ODDS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA": "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "CL": "soccer_uefa_champs_league",
    "EL": "soccer_uefa_europa_league",
    "MLS": "soccer_usa_mls",
}

LEAGUE_NAME_TO_ODDS = {
    "English Premier League": "soccer_epl",
    "Spanish La Liga": "soccer_spain_la_liga",
    "German Bundesliga": "soccer_germany_bundesliga",
    "Italian Serie A": "soccer_italy_serie_a",
    "French Ligue 1": "soccer_france_ligue_one",
    "Dutch Eredivisie": "soccer_netherlands_eredivisie",
    "Portuguese Primeira Liga": "soccer_portugal_primeira_liga",
    "UEFA Champions League": "soccer_uefa_champs_league",
    "UEFA Europa League": "soccer_uefa_europa_league",
    "American Major League Soccer": "soccer_usa_mls",
    "Major League Soccer": "soccer_usa_mls",
}


class SoccerDataError(Exception):
    """Raised when no usable primary or backup soccer schedule can load."""


def get_last_soccer_debug() -> dict[str, Any]:
    """Return source counts from the most recent soccer slate build."""
    return dict(LAST_SOCCER_DEBUG)


def _request_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    required: bool = False,
) -> Any:
    try:
        response = requests.get(
            url, params=params or {}, headers=headers or {}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError) as error:
        if required:
            raise SoccerDataError(
                "The primary soccer schedule is temporarily unavailable."
            ) from error
        raise


def get_football_matches(
    api_key: str, start_date: str, end_date: str, statuses: str | None = None
) -> list[dict[str, Any]]:
    """Fetch covered soccer matches using Football-Data.org API v4."""
    if not api_key:
        raise SoccerDataError("The primary soccer API key is missing from .env.")
    params: dict[str, Any] = {"dateFrom": start_date, "dateTo": end_date}
    if statuses:
        params["status"] = statuses
    payload = _request_json(
        FOOTBALL_DATA_URL,
        params=params,
        headers={"X-Auth-Token": api_key},
        required=True,
    )
    return [item for item in payload.get("matches", []) if isinstance(item, dict)]


def _sportsdb_request(
    api_key: str, endpoint: str, params: dict[str, Any] | None = None
) -> Any:
    """Call one optional TheSportsDB endpoint without exposing its key."""
    if not thesportsdb_enabled():
        raise ValueError("TheSportsDB is disabled.")
    if not api_key:
        raise ValueError("THESPORTSDB_API_KEY is not configured.")
    return _request_json(
        f"{SPORTS_DB_BASE_URL}/{api_key}/{endpoint}", params=params
    )


def check_thesportsdb(api_key: str) -> bool:
    """Return whether TheSportsDB responds for the private /status panel."""
    return check_thesportsdb_connection(api_key)


def check_clubelo() -> bool:
    """Return whether ClubElo responds for owner-only diagnostics."""
    try:
        response = requests.get(f"{CLUBELO_BASE_URL}/Fixtures", timeout=10)
        return response.status_code < 500
    except Exception:
        return False


def check_understat() -> bool:
    """Return whether the optional Understat client appears importable."""
    try:
        __import__("understatapi")
        return True
    except Exception:
        return False


def check_serpapi(api_key: str) -> bool:
    """Return whether SerpApi is configured for optional backup enrichment."""
    return bool(api_key and api_key.strip())


def get_thesportsdb_events(api_key: str, game_date: str) -> list[dict[str, Any]]:
    """Fetch the worldwide soccer schedule used for backup and enrichment."""
    return get_soccer_events(game_date, api_key)


def _sportsdb_kickoff(event: dict[str, Any]) -> str | None:
    """Build an ISO kickoff, preferring the provider's timestamp field."""
    timestamp = event.get("strTimestamp")
    if isinstance(timestamp, str) and timestamp.strip():
        return timestamp.strip()
    event_date, event_time = event.get("dateEvent"), event.get("strTime")
    if isinstance(event_date, str) and event_date:
        time_text = event_time if isinstance(event_time, str) and event_time else "00:00:00"
        combined = f"{event_date}T{time_text}"
        if combined.endswith("Z") or re.search(r"[+-]\d\d:\d\d$", combined):
            return combined
        return f"{combined}Z"
    return None


def _sportsdb_status(event: dict[str, Any]) -> str:
    """Translate backup event status into the primary schedule vocabulary."""
    status = str(event.get("strStatus") or "").lower()
    if status in {"match finished", "finished", "ft"}:
        return "FINISHED"
    if status in {"in progress", "live", "1h", "2h", "ht"}:
        return "IN_PLAY"
    if status in {"postponed", "cancelled", "canceled"}:
        return status.upper()
    return "TIMED" if _sportsdb_kickoff(event) else "SCHEDULED"


def _sportsdb_match(event: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one backup event into a Football-Data-compatible match."""
    home, away = event.get("strHomeTeam"), event.get("strAwayTeam")
    if not isinstance(home, str) or not isinstance(away, str) or not home or not away:
        return None
    home_score, away_score = event.get("intHomeScore"), event.get("intAwayScore")

    def score(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    return {
        "id": event.get("idEvent"),
        "utcDate": _sportsdb_kickoff(event),
        "status": _sportsdb_status(event),
        "homeTeam": {"id": event.get("idHomeTeam"), "name": home},
        "awayTeam": {"id": event.get("idAwayTeam"), "name": away},
        "competition": {
            "id": event.get("idLeague"),
            "name": event.get("strLeague"),
            "code": None,
            "area": {"name": event.get("strCountry"), "code": None},
        },
        "stage": event.get("strSeason"),
        "matchday": event.get("intRound"),
        "score": {
            "fullTime": {
                "home": score(home_score),
                "away": score(away_score),
            }
        },
        "sportsdb_context": {
            "event_id": event.get("idEvent"),
            "league_id": event.get("idLeague"),
            "venue": event.get("strVenue"),
            "round": event.get("intRound"),
            "status": event.get("strStatus"),
        },
    }


def _team_details(api_key: str, team_name: str) -> dict[str, Any] | str:
    """Return a small safe subset of team metadata, including its badge."""
    try:
        payload = _sportsdb_request(api_key, "searchteams.php", {"t": team_name})
        teams = payload.get("teams", []) if isinstance(payload, dict) else []
        team = next(
            (
                item for item in teams or []
                if isinstance(item, dict)
                and _normalize_team(str(item.get("strTeam", ""))) == _normalize_team(team_name)
            ),
            None,
        )
        if not team:
            return UNAVAILABLE
        return {
            "team_id": team.get("idTeam"),
            "name": team.get("strTeam"),
            "short_name": team.get("strTeamShort"),
            "league": team.get("strLeague"),
            "stadium": team.get("strStadium"),
            "country": team.get("strCountry"),
            "badge": team.get("strBadge"),
        }
    except Exception:
        return UNAVAILABLE


def _clubelo_slug(team_name: str) -> str:
    """Create a best-effort ClubElo team slug from a display name."""
    words = [
        word for word in re.findall(r"[A-Za-z0-9]+", team_name)
        if word.lower() not in {"fc", "afc", "cf", "sc", "club", "the"}
    ]
    return "".join(words) or team_name.replace(" ", "")


def _clubelo_rating(team_name: str) -> dict[str, Any] | str:
    """Fetch a compact ClubElo rating for one team when available.

    ClubElo team endpoints are free CSV responses. Team naming is not perfect,
    so a failed lookup simply returns unavailable.
    """
    try:
        response = requests.get(
            f"{CLUBELO_BASE_URL}/{_clubelo_slug(team_name)}",
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "BETGPTAI soccer ratings"},
        )
        response.raise_for_status()
        rows = [line.split(",") for line in response.text.splitlines() if line.strip()]
        if len(rows) < 2:
            return UNAVAILABLE
        headers = rows[0]
        values = rows[-1]
        payload = dict(zip(headers, values))
        elo = payload.get("Elo")
        return {
            "team": team_name,
            "elo": float(elo) if elo not in (None, "") else UNAVAILABLE,
            "rank": payload.get("Rank"),
            "country": payload.get("Country"),
            "from": payload.get("From"),
            "to": payload.get("To"),
        }
    except Exception:
        return UNAVAILABLE


def _understat_team_snapshot(team_name: str) -> dict[str, Any] | str:
    """Return optional Understat xG-style data when an installed client works.

    Understat access can be brittle and sometimes blocks requests. This function
    intentionally treats every problem as unavailable so soccer cards continue.
    """
    try:
        understatapi = __import__("understatapi")
        client_factory = getattr(understatapi, "UnderstatClient", None)
        if client_factory is None:
            return UNAVAILABLE
        client = client_factory()
        league_names = ("EPL", "La_liga", "Bundesliga", "Serie_A", "Ligue_1")
        target = _normalize_team(team_name)
        for league in league_names:
            try:
                teams = client.league(league).get_team_data()
            except Exception:
                continue
            for item in teams.values() if isinstance(teams, dict) else teams:
                if not isinstance(item, dict):
                    continue
                title = item.get("title") or item.get("team_title") or item.get("name")
                if _normalize_team(str(title)) != target:
                    continue
                return {
                    "team": title,
                    "xG": item.get("xG"),
                    "xGA": item.get("xGA"),
                    "shots": item.get("shots"),
                    "shots_on_target": item.get("shotsOnTarget") or item.get("shots_on_target"),
                    "big_chances": item.get("deep") or item.get("big_chances"),
                    "corners": item.get("corners"),
                    "possession": item.get("ppda_att") or item.get("possession"),
                }
    except Exception:
        return UNAVAILABLE
    return UNAVAILABLE


def _serpapi_context(api_key: str, game: dict[str, Any]) -> dict[str, Any] | str:
    """Use SerpApi only as backup context for owner/model internals."""
    if not api_key:
        return UNAVAILABLE
    try:
        query = f"{game.get('away_team')} vs {game.get('home_team')} soccer news injuries standings"
        payload = _request_json(
            SERPAPI_URL,
            params={"engine": "google", "q": query, "api_key": api_key, "num": 3},
        )
        organic = payload.get("organic_results", []) if isinstance(payload, dict) else []
        return {
            "query": query,
            "results": [
                {
                    "title": item.get("title"),
                    "snippet": item.get("snippet"),
                    "link": item.get("link"),
                }
                for item in organic[:3] if isinstance(item, dict)
            ],
        } if organic else UNAVAILABLE
    except Exception:
        return UNAVAILABLE


def enrich_with_advanced_soccer_sources(
    slate: list[dict[str, Any]], serpapi_key: str = ""
) -> list[dict[str, Any]]:
    """Attach optional ClubElo, Understat, and SerpApi context behind the scenes."""
    if not slate:
        return slate
    team_names = {
        str(game.get(key)) for game in slate for key in ("home_team", "away_team")
        if game.get(key)
    }
    clubelo: dict[str, Any] = {}
    understat: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(team_names) * 2))) as executor:
        futures: dict[Any, tuple[str, str]] = {}
        for team in team_names:
            futures[executor.submit(_clubelo_rating, team)] = ("elo", team)
            futures[executor.submit(_understat_team_snapshot, team)] = ("understat", team)
        for future in as_completed(futures):
            kind, team = futures[future]
            try:
                if kind == "elo":
                    clubelo[team] = future.result()
                else:
                    understat[team] = future.result()
            except Exception:
                if kind == "elo":
                    clubelo[team] = UNAVAILABLE
                else:
                    understat[team] = UNAVAILABLE
    for game in slate:
        game["home_elo"] = clubelo.get(str(game.get("home_team")), UNAVAILABLE)
        game["away_elo"] = clubelo.get(str(game.get("away_team")), UNAVAILABLE)
        game["home_advanced"] = understat.get(str(game.get("home_team")), UNAVAILABLE)
        game["away_advanced"] = understat.get(str(game.get("away_team")), UNAVAILABLE)
        game["understat_context"] = {
            "home_team": game["home_advanced"],
            "away_team": game["away_advanced"],
        } if game["home_advanced"] != UNAVAILABLE or game["away_advanced"] != UNAVAILABLE else UNAVAILABLE
        game["injury_context"] = UNAVAILABLE
        game["suspension_context"] = UNAVAILABLE
        game["referee_tendencies"] = UNAVAILABLE
        game["goal_timing"] = UNAVAILABLE
        game["world_cup_context"] = {
            "competition": game.get("competition"),
            "stage": game.get("stage"),
        } if any(
            word in str(game.get("competition", "")).lower()
            for word in ("world cup", "qualifier", "qualification")
        ) else UNAVAILABLE
        game["serpapi_context"] = _serpapi_context(serpapi_key, game)
    return slate


def enrich_with_thesportsdb(
    slate: list[dict[str, Any]],
    api_key: str,
    game_date: str,
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merge matching event, team, league, and badge details when available."""
    if not api_key or not slate:
        for game in slate:
            game["supplemental_soccer_data"] = UNAVAILABLE
        return slate
    try:
        day_events = events if events is not None else get_thesportsdb_events(api_key, game_date)
    except Exception:
        logging.warning("Optional soccer enrichment unavailable", exc_info=True)
        day_events = []

    events_by_match = {
        _match_key(str(event.get("strHomeTeam", "")), str(event.get("strAwayTeam", ""))): event
        for event in day_events
        if event.get("strHomeTeam") and event.get("strAwayTeam")
    }
    league_details: dict[str, dict[str, Any]] = {}
    try:
        payload = _sportsdb_request(api_key, "all_leagues.php")
        for league in payload.get("leagues", []) if isinstance(payload, dict) else []:
            if isinstance(league, dict) and league.get("strLeague"):
                league_details[str(league["strLeague"]).lower()] = {
                    "league_id": league.get("idLeague"),
                    "name": league.get("strLeague"),
                    "sport": league.get("strSport"),
                    "alternate_name": league.get("strLeagueAlternate"),
                }
    except Exception:
        logging.warning("Optional soccer league enrichment unavailable")

    team_names = {
        str(game.get(key)) for game in slate for key in ("home_team", "away_team")
        if game.get(key)
    }
    team_data: dict[str, dict[str, Any] | str] = {}
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(team_names)))) as executor:
        futures = {
            executor.submit(_team_details, api_key, team): team for team in team_names
        }
        for future in as_completed(futures):
            team_data[futures[future]] = future.result()

    for game in slate:
        event = events_by_match.get(
            _match_key(str(game.get("home_team", "")), str(game.get("away_team", "")))
        )
        league_name = str(game.get("competition") or (event or {}).get("strLeague") or "")
        game["supplemental_soccer_data"] = {
            "event": {
                "event_id": (event or {}).get("idEvent"),
                "venue": (event or {}).get("strVenue"),
                "season": (event or {}).get("strSeason"),
                "round": (event or {}).get("intRound"),
                "status": (event or {}).get("strStatus"),
                "poster": (event or {}).get("strPoster"),
            } if event else UNAVAILABLE,
            "home_team": team_data.get(str(game.get("home_team")), UNAVAILABLE),
            "away_team": team_data.get(str(game.get("away_team")), UNAVAILABLE),
            "league": league_details.get(league_name.lower(), UNAVAILABLE),
        }
    return slate


def _completed_score(match: dict[str, Any]) -> tuple[int, int] | None:
    full_time = match.get("score", {}).get("fullTime", {})
    home, away = full_time.get("home"), full_time.get("away")
    if isinstance(home, int) and isinstance(away, int):
        return home, away
    return None


def _team_form(team_id: Any, history: list[dict[str, Any]]) -> dict[str, Any] | str:
    """Calculate recent results, split form, goals, BTTS, and over trends."""
    rows = []
    for match in reversed(history):
        home_id = match.get("homeTeam", {}).get("id")
        away_id = match.get("awayTeam", {}).get("id")
        if team_id not in {home_id, away_id}:
            continue
        score = _completed_score(match)
        if score is None:
            continue
        home_goals, away_goals = score
        is_home = team_id == home_id
        goals_for, goals_against = (
            (home_goals, away_goals) if is_home else (away_goals, home_goals)
        )
        rows.append({
            "venue": "home" if is_home else "away",
            "goals_for": goals_for,
            "goals_against": goals_against,
            "result": "W" if goals_for > goals_against else "D" if goals_for == goals_against else "L",
        })
        if len(rows) == 5:
            break
    if not rows:
        return UNAVAILABLE
    return {
        "matches": len(rows),
        "form": "-".join(row["result"] for row in rows),
        "wins": sum(row["result"] == "W" for row in rows),
        "draws": sum(row["result"] == "D" for row in rows),
        "losses": sum(row["result"] == "L" for row in rows),
        "goals_scored": sum(row["goals_for"] for row in rows),
        "goals_conceded": sum(row["goals_against"] for row in rows),
        "btts_matches": sum(row["goals_for"] > 0 and row["goals_against"] > 0 for row in rows),
        "over_2_5_matches": sum(row["goals_for"] + row["goals_against"] > 2.5 for row in rows),
        "home_split": [row for row in rows if row["venue"] == "home"],
        "away_split": [row for row in rows if row["venue"] == "away"],
    }


def _league_environment(code: str, history: list[dict[str, Any]]) -> dict[str, Any] | str:
    scores = [
        score
        for match in history
        if match.get("competition", {}).get("code") == code
        for score in [_completed_score(match)]
        if score is not None
    ]
    if not scores:
        return UNAVAILABLE
    goals = sum(home + away for home, away in scores)
    return {
        "sample_matches": len(scores),
        "goals_per_match": round(goals / len(scores), 2),
        "btts_rate": round(100 * sum(home > 0 and away > 0 for home, away in scores) / len(scores), 1),
        "over_2_5_rate": round(100 * sum(home + away > 2.5 for home, away in scores) / len(scores), 1),
    }


def _normalize_team(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    ignored = {"fc", "afc", "cf", "sc", "club", "the"}
    return "".join(word for word in words if word not in ignored)


def _match_key(home: str, away: str) -> frozenset[str]:
    return frozenset((_normalize_team(home), _normalize_team(away)))


def _best_prices(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Use every bookmaker internally while returning no sportsbook identity."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for bookmaker in event.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                key = (market.get("key"), outcome.get("name"), outcome.get("point"))
                if key not in best or price > best[key]["price"]:
                    best[key] = {
                        "market": market.get("key"),
                        "outcome": outcome.get("name"),
                        "point": outcome.get("point"),
                        "price": price,
                    }
    # Stable indexes let OpenAI select only a real supplied market. Sportsbook
    # identity has already been removed before this list leaves the data layer.
    return [
        {"index": index, **price}
        for index, price in enumerate(best.values())
    ]


def get_soccer_odds(api_key: str, sport_keys: set[str]) -> list[dict[str, Any]]:
    """Fetch odds only for competitions present on today's covered schedule."""
    if not api_key or not sport_keys:
        return []
    events: list[dict[str, Any]] = []
    for sport_key in sorted(sport_keys):
        try:
            payload = _request_json(
                f"{ODDS_BASE_URL}/sports/{sport_key}/odds/",
                params={
                    "apiKey": api_key,
                    "regions": "us",
                    "markets": "h2h,spreads,totals",
                    "oddsFormat": "american",
                    "dateFormat": "iso",
                },
            )
            if isinstance(payload, list):
                events.extend(item for item in payload if isinstance(item, dict))
        except Exception:
            logging.warning("Soccer odds unavailable for %s", sport_key, exc_info=True)
    return events


def _weather_for_team(team_name: str, kickoff: str | None) -> dict[str, Any] | str:
    """Use club-name geocoding as a best-effort weather location fallback."""
    if not kickoff:
        return UNAVAILABLE
    try:
        places = _request_json(GEOCODING_URL, params={"name": team_name, "count": 1, "language": "en"})
        results = places.get("results", []) if isinstance(places, dict) else []
        if not results:
            return UNAVAILABLE
        place = results[0]
        weather = _request_json(WEATHER_URL, params={
            "latitude": place.get("latitude"), "longitude": place.get("longitude"),
            "hourly": "temperature_2m,wind_speed_10m,precipitation_probability",
            "timezone": "UTC", "forecast_days": 3,
        })
        hourly = weather.get("hourly", {})
        times = hourly.get("time", [])
        target = datetime.fromisoformat(kickoff.replace("Z", "+00:00")).astimezone(timezone.utc)
        parsed = [datetime.fromisoformat(f"{value}+00:00") for value in times]
        if not parsed:
            return UNAVAILABLE
        index = min(range(len(parsed)), key=lambda i: abs((parsed[i] - target).total_seconds()))
        def value(name: str) -> Any:
            values = hourly.get(name, [])
            return values[index] if index < len(values) else None
        return {
            "location": place.get("name"),
            "temperature_c": value("temperature_2m"),
            "wind_kph": value("wind_speed_10m"),
            "precipitation_probability": value("precipitation_probability"),
        }
    except Exception:
        return UNAVAILABLE


def get_soccer_slate(
    football_api_key: str,
    odds_api_key: str,
    *,
    live_only: bool = False,
    game_date: str | None = None,
    sports_db_api_key: str = "",
    serpapi_key: str = "",
    api_football_key: str = "",
) -> list[dict[str, Any]]:
    """Run the free-first soccer stack and return matches from today or tomorrow.

    Official hierarchy:
    Football-Data.org fixtures/results -> TheSportsDB backup -> Weather/Odds
    -> StatsBomb and other hidden enrichment -> OpenAI/Claude analysis.
    API-Football is not required for public soccer cards.
    """
    global LAST_SOCCER_DEBUG
    base_day = date.fromisoformat(game_date) if game_date else date.today()
    allowed_statuses = (
        {"IN_PLAY", "PAUSED"}
        if live_only
        else {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}
    )

    fixtures: list[dict[str, Any]] = []
    selected_day = base_day
    debug_context: dict[str, Any] = {
        "dates_checked": [],
        "football_data_matches": 0,
        "thesportsdb_matches": 0,
        "world_cup_fallback_matches": 0,
        "matches_after_filter": 0,
        "candidate_rejections": [],
    }

    world_cup_matches = get_world_cup_fallback_matches(base_day.isoformat())
    if world_cup_matches:
        fixtures = world_cup_matches
        selected_day = base_day
        debug_context["world_cup_fallback_matches"] = len(world_cup_matches)
        debug_context["matches_after_filter"] = len(fixtures)
        debug_context["candidate_rejections"].append(
            "World Cup fallback schedule active; odds/xG/provider matches not required."
        )

    # Emergency fallback: if today's board is empty, check tomorrow before
    # returning no-card. This catches tournament boards whose lines/schedules
    # populate on the next sports day.
    for current_day in (() if fixtures else (base_day, base_day + timedelta(days=1))):
        current_text = current_day.isoformat()
        debug_context["dates_checked"].append(current_text)
        all_matches: list[dict[str, Any]] = []
        try:
            football_matches = get_football_matches(
                football_api_key, current_text, current_text
            )
            debug_context["football_data_matches"] += len(football_matches)
            all_matches = football_matches
        except SoccerDataError:
            logging.info("Football-Data schedule unavailable; trying soccer backup")

        sportsdb_events: list[dict[str, Any]] = []
        has_allowed_primary_match = any(
            match.get("status") in allowed_statuses for match in all_matches
        )
        if not has_allowed_primary_match and sports_db_api_key:
            try:
                sportsdb_events = get_thesportsdb_events(sports_db_api_key, current_text)
                normalized_matches = [
                    normalized for event in sportsdb_events
                    for normalized in [_sportsdb_match(event)]
                    if normalized is not None
                ]
                debug_context["thesportsdb_matches"] += len(normalized_matches)
                all_matches = normalized_matches
            except Exception:
                logging.info("TheSportsDB soccer schedule backup unavailable")

        fixtures = [
            match for match in all_matches
            if match.get("status") in allowed_statuses
        ]
        if fixtures:
            selected_day = current_day
            debug_context["matches_after_filter"] = len(fixtures)
            break

    if not fixtures:
        LAST_SOCCER_DEBUG = {
            **debug_context,
            "matches_after_filter": 0,
            "odds_markets_found": 0,
            "candidate_rejections": ["Zero scheduled matches across all enabled sources."],
        }
        return []

    if world_cup_mode_enabled() and debug_context.get("world_cup_fallback_matches"):
        slate = [
            {
                **match,
                "football_data_context": UNAVAILABLE,
                "sportsdb_context": UNAVAILABLE,
            }
            for match in fixtures
        ]
        finalized = _finalize_soccer_slate(
            slate, selected_day.isoformat(), sports_db_api_key, serpapi_key,
            debug_context=debug_context,
        )
        LAST_SOCCER_DEBUG = finalized[0].get("soccer_debug_context", debug_context) if finalized else debug_context
        return finalized

    history_start = (selected_day - timedelta(days=10)).isoformat()
    history_end = (selected_day - timedelta(days=1)).isoformat()
    try:
        history = get_football_matches(
            football_api_key, history_start, history_end, "FINISHED"
        )
    except SoccerDataError:
        logging.info("Recent soccer form unavailable")
        history = []

    sport_keys = {
        COMPETITION_TO_ODDS[match.get("competition", {}).get("code")]
        for match in fixtures
        if match.get("competition", {}).get("code") in COMPETITION_TO_ODDS
    }
    sport_keys.update(
        LEAGUE_NAME_TO_ODDS[match.get("competition", {}).get("name")]
        for match in fixtures
        if match.get("competition", {}).get("name") in LEAGUE_NAME_TO_ODDS
    )
    odds = get_soccer_odds(odds_api_key, sport_keys)
    odds_by_match = {
        _match_key(event.get("home_team", ""), event.get("away_team", "")): event
        for event in odds
    }

    slate = []
    for match in fixtures:
        home = match.get("homeTeam", {})
        away = match.get("awayTeam", {})
        competition = match.get("competition", {})
        event = odds_by_match.get(_match_key(home.get("name", ""), away.get("name", "")))
        slate.append({
            "match_id": match.get("id"),
            "kickoff": match.get("utcDate"),
            "status": match.get("status"),
            "minute": match.get("minute"),
            "competition": competition.get("name"),
            "competition_code": competition.get("code"),
            "area_name": competition.get("area", {}).get("name"),
            "area_code": competition.get("area", {}).get("code"),
            "stage": match.get("stage"),
            "matchday": match.get("matchday"),
            "home_team": home.get("name"),
            "away_team": away.get("name"),
            "score": match.get("score"),
            "football_data_context": {
                "match_id": match.get("id"),
                "competition": competition,
                "source_status": match.get("status"),
            } if not match.get("sportsdb_context") else UNAVAILABLE,
            "sportsdb_context": match.get("sportsdb_context", UNAVAILABLE),
            "home_recent": _team_form(home.get("id"), history),
            "away_recent": _team_form(away.get("id"), history),
            "league_environment": _league_environment(competition.get("code"), history),
            "h2h_history": UNAVAILABLE,
            "motivation_context": {
                "stage": match.get("stage"), "matchday": match.get("matchday")
            },
            "corners_profile": UNAVAILABLE,
            "weather": UNAVAILABLE,
            "best_available_prices": _best_prices(event) if event else [],
            "odds_status": "available" if event else UNAVAILABLE,
        })

    finalized = _finalize_soccer_slate(
        slate, selected_day.isoformat(), sports_db_api_key, serpapi_key,
        debug_context=debug_context,
    )
    LAST_SOCCER_DEBUG = finalized[0].get("soccer_debug_context", debug_context) if finalized else debug_context
    return finalized


def _finalize_soccer_slate(
    slate: list[dict[str, Any]], selected_date: str, sports_db_api_key: str,
    serpapi_key: str = "",
    debug_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply shared optional enrichments to primary or fallback fixtures."""
    # StatsBomb is the strongest free analytics enrichment and is attempted
    # before other optional context. Failures stay silent to members.
    try:
        slate = merge_statsbomb_data(slate)
    except Exception:
        logging.info("StatsBomb enrichment unavailable; continuing")
        for game in slate:
            game.setdefault("statsbomb_context", UNAVAILABLE)

    # Match primary fixtures to optional event/team/league metadata. Failures
    # remain isolated and are summarized once by the analysis layer.
    sportsdb_events: list[dict[str, Any]] = []
    if sports_db_api_key:
        try:
            sportsdb_events = get_thesportsdb_events(sports_db_api_key, selected_date)
        except Exception:
            sportsdb_events = []
    slate = enrich_with_thesportsdb(
        slate,
        sports_db_api_key,
        selected_date,
        events=sportsdb_events or None,
    )

    with ThreadPoolExecutor(max_workers=min(6, len(slate))) as executor:
        futures = {
            executor.submit(_weather_for_team, game["home_team"], game["kickoff"]): game
            for game in slate
        }
        for future in as_completed(futures):
            futures[future]["weather"] = future.result()
    try:
        slate = enrich_with_advanced_soccer_sources(
            slate, serpapi_key or os.getenv("SERPAPI_KEY", "")
        )
    except Exception:
        logging.info("Optional advanced soccer enrichment unavailable")
        for game in slate:
            game.setdefault("home_elo", UNAVAILABLE)
            game.setdefault("away_elo", UNAVAILABLE)
            game.setdefault("home_advanced", UNAVAILABLE)
            game.setdefault("away_advanced", UNAVAILABLE)
            game.setdefault("serpapi_context", UNAVAILABLE)
    try:
        slate = merge_fbref_data(slate)
    except Exception:
        logging.info("FBref enrichment unavailable; continuing")
        for game in slate:
            game.setdefault("fbref_context", UNAVAILABLE)
    try:
        slate = enrich_soccer_master_system(slate)
    except Exception:
        logging.info("Soccer Master System engines unavailable")
    # Kickoff order is preserved across the public card and its parlay legs.
    slate.sort(key=lambda game: game_sort_key(game, "kickoff"))
    if slate and debug_context:
        slate[0]["soccer_debug_context"] = {
            **debug_context,
            "odds_markets_found": sum(
                len(game.get("best_available_prices", []))
                for game in slate
                if isinstance(game, dict)
            ),
        }
    return slate


def get_soccer_schedule(
    football_api_key: str, sports_db_api_key: str, game_date: str,
    api_football_key: str = "",
) -> list[dict[str, Any]]:
    """Return lightweight kickoff data with the same primary/backup rules."""
    fallback = get_world_cup_fallback_matches(game_date)
    if fallback:
        schedule = [
            {
                "match_id": match.get("match_id"),
                "kickoff": match.get("kickoff"),
                "status": match.get("status"),
                "home_team": match.get("home_team"),
                "away_team": match.get("away_team"),
            }
            for match in fallback
        ]
        schedule.sort(key=lambda game: game_sort_key(game, "kickoff"))
        return schedule
    try:
        matches = get_football_matches(football_api_key, game_date, game_date)
    except SoccerDataError:
        logging.info("Football-Data soccer schedule unavailable for scheduler")
        matches = []
    if not matches and sports_db_api_key:
        try:
            matches = [
                normalized
                for event in get_thesportsdb_events(sports_db_api_key, game_date)
                for normalized in [_sportsdb_match(event)]
                if normalized is not None
            ]
        except Exception:
            logging.info("TheSportsDB soccer schedule backup unavailable for scheduler")
    if not matches and api_football_key:
        try:
            schedule = get_api_football_schedule(api_football_key, game_date)
            if schedule:
                schedule.sort(key=lambda game: game_sort_key(game, "kickoff"))
                return schedule
        except Exception:
            logging.info("Optional API-Football schedule fallback unavailable")
    allowed = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}
    schedule = [
        {
            "match_id": match.get("id"),
            "kickoff": match.get("utcDate"),
            "status": match.get("status"),
            "home_team": match.get("homeTeam", {}).get("name"),
            "away_team": match.get("awayTeam", {}).get("name"),
        }
        for match in matches
        if match.get("status") in allowed and match.get("utcDate")
    ]
    schedule.sort(key=lambda game: game_sort_key(game, "kickoff"))
    return schedule
