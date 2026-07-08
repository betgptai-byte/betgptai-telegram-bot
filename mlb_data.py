"""Build the MLB schedule, context, weather, and multi-book odds slate."""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any

import requests

from api_sports_baseball import (
    get_api_sports_baseball_schedule,
    merge_api_sports_baseball_data,
)
from fangraphs_data import merge_fangraphs_data
from highlightly_data import merge_highlightly_data
from model_engines import enrich_slate_with_internal_models
from quant_engine import enrich_slate_with_quant_scores
from savant_data import merge_savant_data
from thesportsdb_data import (
    get_baseball_events,
    merge_baseball_metadata,
    thesportsdb_api_key,
)
from weather_data import merge_weather_data
from game_time import game_sort_key


MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
MLB_PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
REQUEST_TIMEOUT = 20
UNAVAILABLE = "unavailable"


TEAM_ALIASES = {
    "arizonadiamondbacks": "diamondbacks",
    "arizonadbacks": "diamondbacks",
    "atlantabraves": "braves",
    "baltimoreorioles": "orioles",
    "bostonredsox": "redsox",
    "chicagocubs": "cubs",
    "chicagowhitesox": "whitesox",
    "cincinnatireds": "reds",
    "clevelandguardians": "guardians",
    "coloradorockies": "rockies",
    "detroittigers": "tigers",
    "houstonastros": "astros",
    "kansascityroyals": "royals",
    "losangelesangels": "angels",
    "laangels": "angels",
    "losangelesdodgers": "dodgers",
    "ladodgers": "dodgers",
    "miamimarlins": "marlins",
    "milwaukeebrewers": "brewers",
    "minnesotatwins": "twins",
    "newyorkmets": "mets",
    "nymets": "mets",
    "newyorkyankees": "yankees",
    "nyyankees": "yankees",
    "oaklandathletics": "athletics",
    "sacramentoathletics": "athletics",
    "athletics": "athletics",
    "philadelphiaphillies": "phillies",
    "pittsburghpirates": "pirates",
    "sandiegopadres": "padres",
    "sanfranciscogiants": "giants",
    "seattlemariners": "mariners",
    "stlouiscardinals": "cardinals",
    "saintlouiscardinals": "cardinals",
    "tampabayrays": "rays",
    "texasrangers": "rangers",
    "torontobluejays": "bluejays",
    "washingtonnationals": "nationals",
}


def _truthy_env(name: str) -> bool:
    """Read opt-in environment flags without surprising defaults."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


class MLBDataError(Exception):
    """A friendly error raised when the required MLB schedule cannot load."""


def _get_json(url: str, params: dict[str, Any]) -> Any:
    """Make an HTTP request and decode JSON without exposing query parameters."""
    try:
        response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        raise MLBDataError(f"A data service returned HTTP status {status}.") from error
    except requests.RequestException as error:
        raise MLBDataError("Could not connect to a data service.") from error
    except ValueError as error:
        raise MLBDataError("A data service returned invalid JSON.") from error


def get_mlb_schedule(game_date: str | None = None) -> list[dict[str, Any]]:
    """Fetch one day's MLB schedule and probable pitchers."""
    selected_date = game_date or date.today().isoformat()
    data = _get_json(MLB_SCHEDULE_URL, {
        "sportId": "1", "hydrate": "probablePitcher,team", "date": selected_date,
    })

    def record_summary(team_side: dict[str, Any]) -> str | None:
        record = team_side.get("leagueRecord") or {}
        if record.get("summary"):
            return record.get("summary")
        wins, losses = record.get("wins"), record.get("losses")
        if wins is not None and losses is not None:
            return f"{wins}-{losses}"
        return None

    games: list[dict[str, Any]] = []
    for date_group in data.get("dates", []):
        for game in date_group.get("games", []):
            away = game.get("teams", {}).get("away", {})
            home = game.get("teams", {}).get("home", {})
            games.append({
                "game_id": game.get("gamePk"),
                "game_pk": game.get("gamePk"),
                "game_time": game.get("gameDate", "Unknown"),
                "status": game.get("status", {}).get("detailedState", "Unknown"),
                "away_team": away.get("team", {}).get("name", "Unknown"),
                "home_team": home.get("team", {}).get("name", "Unknown"),
                "away_team_id": away.get("team", {}).get("id"),
                "home_team_id": home.get("team", {}).get("id"),
                "away_record": record_summary(away),
                "home_record": record_summary(home),
                "away_record_pct": (away.get("leagueRecord") or {}).get("pct"),
                "home_record_pct": (home.get("leagueRecord") or {}).get("pct"),
                "away_pitcher": away.get("probablePitcher", {}).get("fullName", "TBD"),
                "home_pitcher": home.get("probablePitcher", {}).get("fullName", "TBD"),
                "away_pitcher_id": away.get("probablePitcher", {}).get("id"),
                "home_pitcher_id": home.get("probablePitcher", {}).get("id"),
                "away_pitcher_hand": (away.get("probablePitcher", {}).get("pitchHand") or {}).get("code"),
                "home_pitcher_hand": (home.get("probablePitcher", {}).get("pitchHand") or {}).get("code"),
                "venue": game.get("venue", {}).get("name"),
            })
    return games


def get_pitcher_season_stats(pitcher_id: int | None, season: int) -> dict[str, Any] | str:
    """Fetch ERA, WHIP, IP, H, K, BB, and HR allowed for a probable starter."""
    if not pitcher_id:
        return UNAVAILABLE
    data = _get_json(f"{MLB_PEOPLE_URL}/{pitcher_id}/stats", {
        "stats": "season", "group": "pitching", "season": season,
    })
    groups = data.get("stats", [])
    splits = groups[0].get("splits", []) if groups else []
    if not splits:
        return UNAVAILABLE
    stat = splits[0].get("stat", {})
    return {
        "season": season, "ERA": stat.get("era"), "WHIP": stat.get("whip"),
        "IP": stat.get("inningsPitched"), "H": stat.get("hits"),
        "K": stat.get("strikeOuts"), "BB": stat.get("baseOnBalls"),
        "HR": stat.get("homeRuns"),
    }


def get_recent_team_forms(team_ids: set[int], selected_date: str) -> dict[int, dict[str, Any]]:
    """Calculate each team's five most recent completed games in one request."""
    end_date = datetime.fromisoformat(selected_date).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=25)
    data = _get_json(MLB_SCHEDULE_URL, {
        "sportId": "1", "startDate": start_date.isoformat(),
        "endDate": end_date.isoformat(), "hydrate": "linescore",
    })
    completed = [
        game for group in data.get("dates", []) for game in group.get("games", [])
        if game.get("status", {}).get("abstractGameState") == "Final"
    ]
    completed.sort(key=lambda game: game.get("gameDate", ""), reverse=True)
    results: dict[int, list[dict[str, int]]] = {team_id: [] for team_id in team_ids}
    for game in completed:
        away = game.get("teams", {}).get("away", {})
        home = game.get("teams", {}).get("home", {})
        away_id, home_id = away.get("team", {}).get("id"), home.get("team", {}).get("id")
        away_score, home_score = away.get("score"), home.get("score")
        if not isinstance(away_score, int) or not isinstance(home_score, int):
            continue
        if away_id in results and len(results[away_id]) < 5:
            results[away_id].append({"runs_scored": away_score, "runs_allowed": home_score})
        if home_id in results and len(results[home_id]) < 5:
            results[home_id].append({"runs_scored": home_score, "runs_allowed": away_score})
    forms: dict[int, dict[str, Any]] = {}
    for team_id, games in results.items():
        if games:
            wins = sum(game["runs_scored"] > game["runs_allowed"] for game in games)
            forms[team_id] = {
                "games": len(games), "wins": wins, "losses": len(games) - wins,
                "runs_scored": sum(game["runs_scored"] for game in games),
                "runs_allowed": sum(game["runs_allowed"] for game in games),
            }
    return forms


def enrich_mlb_data(slate: list[dict[str, Any]], selected_date: str) -> list[dict[str, Any]]:
    """Add MLB pitcher stats and recent form; optional lookup failures are isolated."""
    season = int(selected_date[:4])
    team_ids = {identifier for game in slate for identifier in
                (game.get("away_team_id"), game.get("home_team_id")) if isinstance(identifier, int)}
    pitcher_ids = {identifier for game in slate for identifier in
                   (game.get("away_pitcher_id"), game.get("home_pitcher_id")) if isinstance(identifier, int)}
    forms: dict[int, dict[str, Any]] = {}
    pitchers: dict[int, Any] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures: dict[Any, tuple[str, Any]] = {}
        if team_ids:
            futures[executor.submit(get_recent_team_forms, team_ids, selected_date)] = ("forms", None)
        for pitcher_id in pitcher_ids:
            futures[executor.submit(get_pitcher_season_stats, pitcher_id, season)] = ("pitcher", pitcher_id)
        for future in as_completed(futures):
            kind, identifier = futures[future]
            try:
                if kind == "forms":
                    forms = future.result()
                else:
                    pitchers[identifier] = future.result()
            except Exception:
                logging.warning("Optional MLB %s lookup failed", kind, exc_info=True)
                if kind == "pitcher":
                    pitchers[identifier] = UNAVAILABLE
    for game in slate:
        game["away_pitcher_stats"] = pitchers.get(game.get("away_pitcher_id"), UNAVAILABLE)
        game["home_pitcher_stats"] = pitchers.get(game.get("home_pitcher_id"), UNAVAILABLE)
        game["away_recent_form"] = forms.get(game.get("away_team_id"), UNAVAILABLE)
        game["home_recent_form"] = forms.get(game.get("home_team_id"), UNAVAILABLE)
    # Every consumer receives games in official Eastern chronological order.
    slate.sort(key=game_sort_key)
    return slate


def get_mlb_odds(odds_api_key: str) -> list[dict[str, Any]]:
    """Fetch h2h, spreads, and totals from all U.S. bookmakers."""
    if not odds_api_key:
        raise MLBDataError("ODDS_API_KEY is missing from .env.")
    data = _get_json(ODDS_URL, {
        "apiKey": odds_api_key, "regions": "us", "markets": "h2h,spreads,totals",
        "oddsFormat": "american", "dateFormat": "iso",
    })
    if not isinstance(data, list):
        raise MLBDataError("The Odds API returned an unexpected response.")
    return data


def _normalize_team(team_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", team_name.lower())
    return TEAM_ALIASES.get(normalized, normalized)


def _game_key(away_team: str, home_team: str) -> frozenset[str]:
    return frozenset((_normalize_team(away_team), _normalize_team(home_team)))


def _clean_bookmakers(odds_game: dict[str, Any]) -> list[dict[str, Any]]:
    """Retain all books internally so the best line can be selected later."""
    cleaned = []
    for bookmaker in odds_game.get("bookmakers", []):
        markets = []
        for market in bookmaker.get("markets", []):
            markets.append({
                "market": market.get("key"), "last_update": market.get("last_update"),
                "outcomes": [{"name": item.get("name"), "price": item.get("price"),
                              "point": item.get("point"),
                              "description": item.get("description")}
                             for item in market.get("outcomes", [])],
            })
        cleaned.append({"key": bookmaker.get("key"), "name": bookmaker.get("title"),
                        "last_update": bookmaker.get("last_update"), "markets": markets})
    return cleaned


def _best_available_prices(bookmakers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the highest price for each distinct market/outcome/point."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for bookmaker in bookmakers:
        for market in bookmaker["markets"]:
            for outcome in market["outcomes"]:
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                key = (
                    market.get("market"), outcome.get("name"),
                    outcome.get("point"), outcome.get("description"),
                )
                if key not in best or price > best[key]["price"]:
                    best[key] = {
                        "market": market.get("market"), "outcome": outcome.get("name"),
                        "point": outcome.get("point"), "price": price,
                        "description": outcome.get("description"),
                        # Attribution remains internal and is stripped before Telegram.
                        "bookmaker_key": bookmaker.get("key"), "bookmaker": bookmaker.get("name"),
                    }
    return list(best.values())


def _parse_api_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _closest_odds_game(game: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    game_time = _parse_api_time(game.get("game_time"))
    if game_time is None:
        return candidates.pop(0)
    match = min(candidates, key=lambda candidate: abs((game_time - _parse_api_time(
        candidate.get("commence_time"))).total_seconds()) if _parse_api_time(
        candidate.get("commence_time")) else float("inf"))
    candidates.remove(match)
    return match


def combine_schedule_and_odds(schedule: list[dict[str, Any]], odds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match odds events to MLB games by normalized team names and start time."""
    by_game: dict[frozenset[str], list[dict[str, Any]]] = {}
    for odds_game in odds:
        by_game.setdefault(_game_key(odds_game.get("away_team", ""), odds_game.get("home_team", "")), []).append(odds_game)
    combined = []
    for game in schedule:
        odds_game = _closest_odds_game(game, by_game.get(_game_key(game["away_team"], game["home_team"]), []))
        bookmakers = _clean_bookmakers(odds_game) if odds_game else []
        best_prices = _best_available_prices(bookmakers)
        combined.append({**game, "odds_event_id": odds_game.get("id") if odds_game else None,
                         "best_available_prices": best_prices,
                         "bookmakers": bookmakers,
                         "odds_status": "available" if odds_game else UNAVAILABLE,
                         "market_context": _market_context_from_prices(best_prices)})
    return combined


def _attach_unavailable_odds(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for game in slate:
        game.update({"odds_event_id": None, "best_available_prices": [],
                     "bookmakers": [], "odds_status": UNAVAILABLE,
                     "market_context": _market_context_from_prices([])})
    return slate


def _market_context_from_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact market summary stored on each game for admin diagnostics."""
    context = {"ML": [], "RL": [], "total": [], "team_totals": [], "odds_found": bool(prices)}
    for price in prices:
        market = price.get("market")
        label = price.get("description") or price.get("outcome")
        point = price.get("point")
        american = price.get("price")
        if market == "h2h":
            context["ML"].append({"label": label, "odds": american})
        elif market == "spreads":
            context["RL"].append({"label": label, "line": point, "odds": american})
        elif market == "totals":
            context["total"].append({"label": label, "line": point, "odds": american})
        elif market == "team_totals":
            context["team_totals"].append({"label": label, "line": point, "odds": american})
    return context


def odds_debug_payload(odds_api_key: str, selected_date: str) -> dict[str, Any]:
    """Owner-only diagnostics for The Odds API MLB game matching."""
    payload: dict[str, Any] = {
        "odds_api_key_loaded": bool(str(odds_api_key or "").strip()),
        "odds_api_status_code": None,
        "sport_key": "baseball_mlb",
        "markets_requested": "h2h,spreads,totals",
        "games_returned": 0,
        "matched_to_mlb_game_pk": 0,
        "unmatched_odds_games": [],
        "unmatched_mlb_games": [],
        "last_error": "",
        "errors": [],
    }
    try:
        schedule = get_mlb_schedule(selected_date)
    except Exception as error:
        schedule = []
        payload["errors"].append(f"MLB schedule unavailable: {error}")
    try:
        if not odds_api_key:
            raise MLBDataError("ODDS_API_KEY is missing from .env.")
        response = requests.get(ODDS_URL, params={
            "apiKey": odds_api_key,
            "regions": "us",
            "markets": "h2h,spreads,totals",
            "oddsFormat": "american",
            "dateFormat": "iso",
        }, timeout=REQUEST_TIMEOUT)
        payload["odds_api_status_code"] = response.status_code
        response.raise_for_status()
        decoded = response.json()
        if not isinstance(decoded, list):
            raise MLBDataError("The Odds API returned an unexpected response.")
        odds = decoded
    except Exception as error:
        odds = []
        redacted_error = _redact_secret(str(error))
        payload["last_error"] = redacted_error
        payload["errors"].append(f"Odds fetch unavailable: {redacted_error}")
    payload["games_returned"] = len(odds)
    odds_keys = {
        _game_key(odds_game.get("away_team", ""), odds_game.get("home_team", "")): odds_game
        for odds_game in odds
    }
    matched_keys = set()
    for game in schedule:
        key = _game_key(game.get("away_team", ""), game.get("home_team", ""))
        if key in odds_keys:
            matched_keys.add(key)
            payload["matched_to_mlb_game_pk"] += 1
        else:
            payload["unmatched_mlb_games"].append({
                "game_pk": game.get("game_pk"),
                "away_team": game.get("away_team"),
                "home_team": game.get("home_team"),
                "normalized_key": sorted(key),
            })
    for key, odds_game in odds_keys.items():
        if key not in matched_keys:
            payload["unmatched_odds_games"].append({
                "id": odds_game.get("id"),
                "away_team": odds_game.get("away_team"),
                "home_team": odds_game.get("home_team"),
                "commence_time": odds_game.get("commence_time"),
                "normalized_key": sorted(key),
            })
    return payload


def _redact_secret(text: str) -> str:
    """Remove API key values from exception strings before admin display."""
    text = re.sub(r"apiKey=[^&\\s]+", "apiKey=REDACTED", text)
    text = re.sub(r"api_key=[^&\\s]+", "api_key=REDACTED", text, flags=re.I)
    return text


def _sportsdb_baseball_schedule(selected_date: str) -> list[dict[str, Any]]:
    """Normalize optional TheSportsDB baseball schedule backup rows."""
    events = get_baseball_events(selected_date, thesportsdb_api_key())
    games: list[dict[str, Any]] = []
    for event in events:
        home = event.get("strHomeTeam")
        away = event.get("strAwayTeam")
        if not home or not away:
            continue
        games.append({
            "game_id": event.get("idEvent"),
            "game_time": event.get("strTimestamp")
            or f"{event.get('dateEvent', selected_date)}T{event.get('strTime') or '00:00:00'}Z",
            "status": event.get("strStatus") or "Scheduled",
            "away_team": away,
            "home_team": home,
            "away_team_id": event.get("idAwayTeam"),
            "home_team_id": event.get("idHomeTeam"),
            "away_pitcher": "TBD",
            "home_pitcher": "TBD",
            "away_pitcher_id": None,
            "home_pitcher_id": None,
            "venue": event.get("strVenue"),
            "thesportsdb_metadata": {
                "event_id": event.get("idEvent"),
                "league": event.get("strLeague"),
                "league_id": event.get("idLeague"),
                "event_thumb": event.get("strThumb"),
                "venue": event.get("strVenue"),
            },
        })
    return games


def get_combined_slate(
    odds_api_key: str, game_date: str | None = None, highlightly_api_key: str = ""
) -> list[dict[str, Any]]:
    """Run MLB -> Savant -> Highlightly -> weather -> odds safely."""
    selected_date = game_date or date.today().isoformat()
    api_sports_enabled = _truthy_env("API_SPORTS_BASEBALL_ENABLED")
    primary_mlb_schedule = True
    mlb_stats_failed = False
    try:
        slate = get_mlb_schedule(selected_date)
    except Exception:
        if api_sports_enabled:
            logging.warning("MLB Stats schedule unavailable; trying optional backup", exc_info=True)
        else:
            logging.warning("MLB Stats schedule unavailable; API-Sports backup disabled", exc_info=True)
        slate = []
        primary_mlb_schedule = False
        mlb_stats_failed = True

    if not slate and mlb_stats_failed and api_sports_enabled:
        # API-Sports Baseball is a backup only. It is not used when MLB Stats
        # has a schedule, and it never replaces Savant or the betting model.
        try:
            slate = get_api_sports_baseball_schedule(selected_date)
            primary_mlb_schedule = False
        except Exception:
            logging.warning("API-Sports Baseball schedule backup unavailable", exc_info=True)
            slate = []
    if not slate and mlb_stats_failed:
        try:
            slate = _sportsdb_baseball_schedule(selected_date)
            primary_mlb_schedule = False
        except Exception:
            logging.debug("TheSportsDB baseball schedule backup unavailable; continuing", exc_info=True)
            slate = []
    if not slate:
        return []

    # MLB-derived pitcher and form context belongs to the first pipeline stage.
    # Backup schedule rows do not use MLB Stats team/player IDs, so skip those
    # lookups when we are operating from the optional backup source.
    if primary_mlb_schedule:
        slate = enrich_mlb_data(slate, selected_date)
    else:
        for game in slate:
            game.setdefault("away_pitcher_stats", UNAVAILABLE)
            game.setdefault("home_pitcher_stats", UNAVAILABLE)
            game.setdefault("away_recent_form", UNAVAILABLE)
            game.setdefault("home_recent_form", UNAVAILABLE)

    # Statcast expected/contact metrics are predictive enrichment. Baseball
    # Savant is optional, so a changed leaderboard never blocks the card.
    try:
        slate = merge_savant_data(slate, selected_date)
    except Exception:
        logging.warning("Baseball Savant unavailable; continuing", exc_info=True)
        for game in slate:
            game["savant"] = UNAVAILABLE

    try:
        slate = merge_fangraphs_data(slate, selected_date)
    except Exception:
        # FanGraphs commonly blocks pybaseball with HTTP 403. It is bonus
        # context only, so MLB cards continue quietly without it.
        logging.debug("FanGraphs enrichment unavailable; continuing", exc_info=True)
        for game in slate:
            game.setdefault("fangraphs", UNAVAILABLE)

    try:
        slate = merge_baseball_metadata(slate, selected_date, thesportsdb_api_key())
    except Exception:
        logging.debug("TheSportsDB baseball metadata unavailable; continuing", exc_info=True)
        for game in slate:
            game.setdefault("thesportsdb_metadata", UNAVAILABLE)

    try:
        slate = merge_highlightly_data(slate, highlightly_api_key, selected_date)
    except Exception:
        logging.debug("Highlightly optional enrichment unavailable; continuing", exc_info=True)
        for game in slate:
            game["highlightly"] = UNAVAILABLE

    try:
        slate = merge_weather_data(slate)
    except Exception:
        logging.warning("Weather unavailable; continuing", exc_info=True)
        for game in slate:
            game.setdefault("weather", UNAVAILABLE)
            game.setdefault("park_factor", "neutral")

    try:
        odds = get_mlb_odds(odds_api_key)
        slate = combine_schedule_and_odds(slate, odds)
    except Exception as error:
        # Do not use exc_info here: requests tracebacks include the full URL,
        # which can expose ODDS_API_KEY in logs.
        logging.warning("Odds unavailable; continuing with schedule data: %s", error)
        slate = _attach_unavailable_odds(slate)
    if api_sports_enabled:
        try:
            slate = merge_api_sports_baseball_data(slate, selected_date)
        except Exception:
            logging.debug("API-Sports Baseball optional enrichment unavailable; continuing", exc_info=True)
    try:
        # Internal model engines use the finished slate, including odds and
        # Savant context. Their details are hidden from member-facing cards but
        # are available to analysts and owner-only reports.
        slate = enrich_slate_with_internal_models(slate)
    except Exception:
        logging.warning("Internal model engines unavailable; continuing", exc_info=True)
    try:
        # BETGPTAI v20 is the official deterministic quant layer. It scores the
        # verified API slate before AI sees it, so AI can rank/explain but never
        # invent missing stats.
        slate = enrich_slate_with_quant_scores(slate, selected_date)
    except Exception:
        logging.warning("BETGPTAI v20 quant engine unavailable; continuing", exc_info=True)
    return slate
