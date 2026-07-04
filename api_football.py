"""Primary API-Football soccer adapter for BETGPTAI.

API-Football is used as the first soccer data source when API_FOOTBALL_KEY is
configured.  Every endpoint is best-effort: if a plan limit, missing coverage,
or temporary API failure occurs, the caller can safely fall back to
Football-Data.org and TheSportsDB.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import requests


BASE_URL = "https://v3.football.api-sports.io"
REQUEST_TIMEOUT = 20
UNAVAILABLE = "unavailable"
_QUOTA_COOLDOWN_UNTIL: datetime | None = None
_QUOTA_LOGGED = False


class APIFootballError(Exception):
    """Raised when the primary API-Football source cannot provide fixtures."""


def _quota_limited() -> bool:
    """Return True when the API already told us today's quota is exhausted."""
    return _QUOTA_COOLDOWN_UNTIL is not None and datetime.now() < _QUOTA_COOLDOWN_UNTIL


def _mark_quota_limited(errors: Any) -> None:
    """Stop repeated API-Football calls after the daily limit is reached."""
    global _QUOTA_COOLDOWN_UNTIL, _QUOTA_LOGGED
    _QUOTA_COOLDOWN_UNTIL = datetime.now().replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(minutes=5)
    if not _QUOTA_LOGGED:
        logging.info(
            "API-Football daily request limit reached; disabling optional API-Football calls until tomorrow. errors=%s",
            errors,
        )
        _QUOTA_LOGGED = True


def api_football_quota_limited() -> bool:
    """Expose quota state for owner-only status/debug commands."""
    return _quota_limited()


def api_football_status_label(api_key: str) -> str:
    """Return clean optional-provider status wording without forcing failures."""
    if not api_key:
        return "➖ Not configured"
    if _quota_limited():
        return "➖ Daily limit reached"
    return "✅ Available" if api_football_available(api_key) else "➖ Optional unavailable"


def _get_json(api_key: str, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call API-Football without leaking the key in exceptions."""
    if not api_key:
        raise APIFootballError("API_FOOTBALL_KEY is missing from .env.")
    if _quota_limited():
        raise APIFootballError("API-Football daily request limit already reached.")
    try:
        response = requests.get(
            f"{BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}",
            params=params or {},
            headers={"x-apisports-key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise APIFootballError("API-Football is temporarily unavailable.") from error
    if not isinstance(payload, dict):
        raise APIFootballError("API-Football returned an unexpected response.")
    errors = payload.get("errors")
    if errors:
        error_text = str(errors).lower()
        if "request limit" in error_text or "limit for the day" in error_text:
            _mark_quota_limited(errors)
            raise APIFootballError("API-Football daily request limit reached.")
        logging.info("Optional API-Football returned errors: %s", errors)
    return payload


def api_football_available(api_key: str) -> bool:
    """Return whether API-Football accepts a lightweight status request."""
    if _quota_limited():
        return False
    try:
        payload = _get_json(api_key, "status")
        return isinstance(payload.get("response"), dict) or "results" in payload
    except Exception:
        return False


def _response_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = payload.get("response", [])
    return [item for item in response if isinstance(item, dict)] if isinstance(response, list) else []


def _fixture_status(item: dict[str, Any]) -> str:
    short = str(item.get("fixture", {}).get("status", {}).get("short") or "").upper()
    if short in {"1H", "2H", "HT", "ET", "BT", "P", "LIVE"}:
        return "IN_PLAY"
    if short in {"FT", "AET", "PEN"}:
        return "FINISHED"
    if short in {"PST", "CANC", "ABD", "AWD", "WO"}:
        return short
    return "TIMED"


def _score(item: dict[str, Any]) -> dict[str, Any]:
    goals = item.get("goals", {}) if isinstance(item.get("goals"), dict) else {}
    return {
        "fullTime": {
            "home": goals.get("home"),
            "away": goals.get("away"),
        }
    }


def _normalize_fixture(item: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize API-Football fixture shape into the existing soccer slate."""
    fixture = item.get("fixture", {}) if isinstance(item.get("fixture"), dict) else {}
    teams = item.get("teams", {}) if isinstance(item.get("teams"), dict) else {}
    league = item.get("league", {}) if isinstance(item.get("league"), dict) else {}
    home = teams.get("home", {}) if isinstance(teams.get("home"), dict) else {}
    away = teams.get("away", {}) if isinstance(teams.get("away"), dict) else {}
    if not home.get("name") or not away.get("name"):
        return None
    return {
        "match_id": fixture.get("id"),
        "kickoff": fixture.get("date"),
        "status": _fixture_status(item),
        "minute": fixture.get("status", {}).get("elapsed"),
        "competition": league.get("name"),
        "competition_code": league.get("type"),
        "league_id": league.get("id"),
        "season": league.get("season"),
        "area_name": league.get("country"),
        "area_code": None,
        "stage": league.get("round"),
        "matchday": None,
        "home_team": home.get("name"),
        "away_team": away.get("name"),
        "home_team_id": home.get("id"),
        "away_team_id": away.get("id"),
        "score": _score(item),
        "home_recent": UNAVAILABLE,
        "away_recent": UNAVAILABLE,
        "league_environment": UNAVAILABLE,
        "h2h_history": UNAVAILABLE,
        "motivation_context": {
            "stage": league.get("round"),
            "league": league.get("name"),
            "country": league.get("country"),
        },
        "corners_profile": UNAVAILABLE,
        "weather": UNAVAILABLE,
        "best_available_prices": [],
        "odds_status": UNAVAILABLE,
        "api_football_context": {
            "fixture_id": fixture.get("id"),
            "league_id": league.get("id"),
            "season": league.get("season"),
            "venue": (fixture.get("venue") or {}).get("name") if isinstance(fixture.get("venue"), dict) else None,
            "referee": fixture.get("referee"),
            "round": league.get("round"),
            "status": fixture.get("status"),
        },
    }


def get_api_football_fixtures(
    api_key: str, game_date: str | None = None, live_only: bool = False
) -> list[dict[str, Any]]:
    """Fetch fixtures from API-Football for one day or current live board."""
    if live_only:
        payload = _get_json(api_key, "fixtures", {"live": "all"})
    else:
        payload = _get_json(api_key, "fixtures", {"date": game_date or date.today().isoformat()})
    fixtures = [_normalize_fixture(item) for item in _response_list(payload)]
    return [fixture for fixture in fixtures if fixture is not None]


def _american_from_decimal(value: Any) -> int | None:
    """Convert decimal odds strings from API-Football to American odds."""
    try:
        decimal = float(value)
    except (TypeError, ValueError):
        return None
    if decimal <= 1:
        return None
    if decimal >= 2:
        return round((decimal - 1) * 100)
    return round(-100 / (decimal - 1))


def _api_football_prices(api_key: str, fixture_id: Any) -> list[dict[str, Any]]:
    """Read odds markets when the user's API-Football plan exposes them."""
    if not fixture_id:
        return []
    try:
        payload = _get_json(api_key, "odds", {"fixture": fixture_id})
    except Exception:
        return []
    prices: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for event in _response_list(payload):
        for bookmaker in event.get("bookmakers", []) or []:
            for bet in bookmaker.get("bets", []) or []:
                market_name = str(bet.get("name") or "").lower()
                for value in bet.get("values", []) or []:
                    outcome = str(value.get("value") or "")
                    price = _american_from_decimal(value.get("odd"))
                    if price is None:
                        continue
                    mapped = _map_market(market_name, outcome)
                    if not mapped:
                        continue
                    market, mapped_outcome, point = mapped
                    key = (market, mapped_outcome, point)
                    if key in seen:
                        continue
                    seen.add(key)
                    prices.append({
                        "index": len(prices),
                        "market": market,
                        "outcome": mapped_outcome,
                        "point": point,
                        "price": price,
                    })
    return prices


def _map_market(market_name: str, outcome: str) -> tuple[str, str, float | None] | None:
    """Map common API-Football markets to the bot's existing market labels."""
    clean = outcome.strip()
    lower = clean.lower()
    if market_name in {"match winner", "1x2", "winner"}:
        if lower == "draw":
            return None
        return ("h2h", clean, None)
    if "both teams score" in market_name or "btts" in market_name:
        if lower in {"yes", "no"}:
            return ("btts", "Yes" if lower == "yes" else "No", None)
    if "goals over/under" in market_name or market_name in {"goals over/under", "total goals"}:
        parts = clean.split()
        if len(parts) >= 2:
            try:
                return ("totals", parts[0].title(), float(parts[-1]))
            except ValueError:
                return None
    if "double chance" in market_name:
        return ("double_chance", clean, None)
    return None


def _fixture_statistics(api_key: str, fixture_id: Any) -> dict[str, Any] | str:
    try:
        payload = _get_json(api_key, "fixtures/statistics", {"fixture": fixture_id})
    except Exception:
        return UNAVAILABLE
    teams = {}
    for item in _response_list(payload):
        team = item.get("team", {}) if isinstance(item.get("team"), dict) else {}
        stats = {}
        for row in item.get("statistics", []) or []:
            if isinstance(row, dict):
                stats[str(row.get("type"))] = row.get("value")
        name = team.get("name")
        if name:
            teams[name] = stats
    return teams or UNAVAILABLE


def _last_five(api_key: str, team_id: Any) -> dict[str, Any] | str:
    if not team_id:
        return UNAVAILABLE
    try:
        payload = _get_json(api_key, "fixtures", {"team": team_id, "last": 5})
    except Exception:
        return UNAVAILABLE
    rows = _response_list(payload)
    if not rows:
        return UNAVAILABLE
    wins = draws = losses = goals_for = goals_against = btts = overs = clean = 0
    for item in rows:
        teams = item.get("teams", {}) if isinstance(item.get("teams"), dict) else {}
        home = teams.get("home", {}) if isinstance(teams.get("home"), dict) else {}
        goals = item.get("goals", {}) if isinstance(item.get("goals"), dict) else {}
        is_home = home.get("id") == team_id
        gf = goals.get("home") if is_home else goals.get("away")
        ga = goals.get("away") if is_home else goals.get("home")
        if not isinstance(gf, int) or not isinstance(ga, int):
            continue
        goals_for += gf
        goals_against += ga
        wins += gf > ga
        draws += gf == ga
        losses += gf < ga
        btts += gf > 0 and ga > 0
        overs += gf + ga > 2.5
        clean += ga == 0
    played = wins + draws + losses
    return {
        "matches": played,
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_scored": goals_for,
        "goals_conceded": goals_against,
        "btts_matches": btts,
        "over_2_5_matches": overs,
        "clean_sheets": clean,
    } if played else UNAVAILABLE


def _team_statistics(api_key: str, league_id: Any, season: Any, team_id: Any) -> dict[str, Any] | str:
    if not league_id or not season or not team_id:
        return UNAVAILABLE
    try:
        payload = _get_json(api_key, "teams/statistics", {
            "league": league_id, "season": season, "team": team_id,
        })
    except Exception:
        return UNAVAILABLE
    response = payload.get("response")
    return response if isinstance(response, dict) and response else UNAVAILABLE


def _h2h(api_key: str, home_id: Any, away_id: Any) -> list[dict[str, Any]] | str:
    if not home_id or not away_id:
        return UNAVAILABLE
    try:
        payload = _get_json(api_key, "fixtures/headtohead", {"h2h": f"{home_id}-{away_id}", "last": 5})
    except Exception:
        return UNAVAILABLE
    return _response_list(payload)[:5] or UNAVAILABLE


def _fixture_list_endpoint(api_key: str, endpoint: str, fixture_id: Any) -> Any:
    if not fixture_id:
        return UNAVAILABLE
    try:
        payload = _get_json(api_key, endpoint, {"fixture": fixture_id})
    except Exception:
        return UNAVAILABLE
    rows = _response_list(payload)
    return rows if rows else UNAVAILABLE


def _standings(api_key: str, league_id: Any, season: Any) -> Any:
    if not league_id or not season:
        return UNAVAILABLE
    try:
        payload = _get_json(api_key, "standings", {"league": league_id, "season": season})
    except Exception:
        return UNAVAILABLE
    return payload.get("response") or UNAVAILABLE


def enrich_api_football_slate(slate: list[dict[str, Any]], api_key: str) -> list[dict[str, Any]]:
    """Add API-Football context to normalized fixtures."""
    for game in slate:
        fixture_id = game.get("match_id")
        league_id = game.get("league_id")
        season = game.get("season")
        home_id = game.get("home_team_id")
        away_id = game.get("away_team_id")
        prices = _api_football_prices(api_key, fixture_id)
        if prices:
            game["best_available_prices"] = prices
            game["odds_status"] = "available"
        game["home_recent"] = _last_five(api_key, home_id)
        game["away_recent"] = _last_five(api_key, away_id)
        fixture_stats = _fixture_statistics(api_key, fixture_id)
        game["api_football_context"] = {
            **(game.get("api_football_context") if isinstance(game.get("api_football_context"), dict) else {}),
            "fixture_statistics": fixture_stats,
            "home_team_statistics": _team_statistics(api_key, league_id, season, home_id),
            "away_team_statistics": _team_statistics(api_key, league_id, season, away_id),
            "standings": _standings(api_key, league_id, season),
            "h2h": _h2h(api_key, home_id, away_id),
            "injuries": _fixture_list_endpoint(api_key, "injuries", fixture_id),
            "lineups": _fixture_list_endpoint(api_key, "fixtures/lineups", fixture_id),
            "player_statistics": _fixture_list_endpoint(api_key, "fixtures/players", fixture_id),
        }
        game["h2h_history"] = game["api_football_context"].get("h2h", UNAVAILABLE)
        _apply_statistics_to_game(game, fixture_stats)
    return slate


def _stat_value(stats: dict[str, Any], *names: str) -> Any:
    normalized = {str(key).lower(): value for key, value in stats.items()}
    for name in names:
        key = name.lower()
        if key in normalized:
            return normalized[key]
    return UNAVAILABLE


def _apply_statistics_to_game(game: dict[str, Any], fixture_stats: Any) -> None:
    """Convert live/finished fixture statistics into existing hidden fields."""
    if not isinstance(fixture_stats, dict):
        return
    home_name, away_name = str(game.get("home_team")), str(game.get("away_team"))
    home_stats = fixture_stats.get(home_name)
    away_stats = fixture_stats.get(away_name)
    if not isinstance(home_stats, dict) or not isinstance(away_stats, dict):
        return
    game["home_advanced"] = {
        **(game.get("home_advanced") if isinstance(game.get("home_advanced"), dict) else {}),
        "shots": _stat_value(home_stats, "Total Shots"),
        "shots_on_target": _stat_value(home_stats, "Shots on Goal"),
        "possession": _stat_value(home_stats, "Ball Possession"),
        "corners": _stat_value(home_stats, "Corner Kicks"),
    }
    game["away_advanced"] = {
        **(game.get("away_advanced") if isinstance(game.get("away_advanced"), dict) else {}),
        "shots": _stat_value(away_stats, "Total Shots"),
        "shots_on_target": _stat_value(away_stats, "Shots on Goal"),
        "possession": _stat_value(away_stats, "Ball Possession"),
        "corners": _stat_value(away_stats, "Corner Kicks"),
    }
    game["corners_profile"] = {
        "home_corners": _stat_value(home_stats, "Corner Kicks"),
        "away_corners": _stat_value(away_stats, "Corner Kicks"),
    }
    game["referee_tendencies"] = {
        "home_yellow_cards": _stat_value(home_stats, "Yellow Cards"),
        "away_yellow_cards": _stat_value(away_stats, "Yellow Cards"),
        "home_red_cards": _stat_value(home_stats, "Red Cards"),
        "away_red_cards": _stat_value(away_stats, "Red Cards"),
    }


def get_api_football_slate(
    api_key: str, game_date: str | None = None, live_only: bool = False
) -> list[dict[str, Any]]:
    """Fetch and enrich API-Football fixtures for the soccer pipeline."""
    slate = get_api_football_fixtures(api_key, game_date, live_only)
    if not slate:
        return []
    return enrich_api_football_slate(slate, api_key)


def get_api_football_schedule(
    api_key: str, game_date: str, live_only: bool = False
) -> list[dict[str, Any]]:
    """Return lightweight API-Football schedule rows for the scheduler."""
    return [
        {
            "match_id": game.get("match_id"),
            "kickoff": game.get("kickoff"),
            "status": game.get("status"),
            "home_team": game.get("home_team"),
            "away_team": game.get("away_team"),
        }
        for game in get_api_football_fixtures(api_key, game_date, live_only)
        if game.get("kickoff")
    ]
