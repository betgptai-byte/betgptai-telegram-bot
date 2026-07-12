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
from time_utils import mlb_local_game_date, mlb_utc_query_window, to_et


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
    from api.sharp_client import _normalize_team as normalize_provider_team
    normalized = normalize_provider_team(team_name)
    return TEAM_ALIASES.get(normalized, normalized)


def _game_key(away_team: str, home_team: str) -> tuple[str, str]:
    """Ordered normalized matchup key: away@home."""
    return _normalize_team(away_team), _normalize_team(home_team)


def _game_key_text(away_team: str, home_team: str) -> str:
    away, home = _game_key(away_team, home_team)
    return f"{away}@{home}"


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
    match, _, _ = _closest_odds_game_detail(game, candidates)
    return match


def _closest_odds_game_detail(
    game: dict[str, Any], candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, float | None, str | None]:
    if not candidates:
        return None, None, "team_name_mismatch"
    game_time = _parse_api_time(game.get("game_time"))
    if game_time is None:
        return None, None, "missing_mlb_start_time"
    match = min(candidates, key=lambda candidate: abs((game_time - _parse_api_time(
        candidate.get("commence_time"))).total_seconds()) if _parse_api_time(
        candidate.get("commence_time")) else float("inf"))
    match_time = _parse_api_time(match.get("commence_time"))
    if match_time is None:
        return None, None, "missing_sharp_start_time"
    difference_minutes = abs((game_time - match_time).total_seconds()) / 60.0
    if difference_minutes > 12 * 60:
        return None, difference_minutes, "time_window_mismatch"
    candidates.remove(match)
    return match, difference_minutes, None


def combine_schedule_and_odds(schedule: list[dict[str, Any]], odds: list[dict[str, Any]], card_date: str | None = None) -> list[dict[str, Any]]:
    """Match odds events to MLB games by normalized team names and start time."""
    from api.sharp_odds_client import build_game_market_context
    from time_utils import mlb_local_game_date

    by_game: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for odds_game in odds:
        if card_date and mlb_local_game_date(odds_game.get("commence_time")) != card_date:
            continue
        by_game.setdefault(_game_key(odds_game.get("away_team", ""), odds_game.get("home_team", "")), []).append(odds_game)
    provider = _detect_odds_provider(odds)
    combined = []
    for game in schedule:
        if card_date and mlb_local_game_date(game.get("game_time")) != card_date:
            continue
        odds_game = _closest_odds_game(game, by_game.get(_game_key(game["away_team"], game["home_team"]), []))
        bookmakers = _clean_bookmakers(odds_game) if odds_game else []
        best_prices = _best_available_prices(bookmakers)
        game_row = {**game, "odds_event_id": odds_game.get("id") if odds_game else None,
                    "best_available_prices": best_prices,
                    "bookmakers": bookmakers,
                    "odds_status": "available" if odds_game else UNAVAILABLE}
        game_row["market_context"] = build_game_market_context(game_row, best_prices, provider)
        combined.append(game_row)
    return combined


def _detect_odds_provider(odds: list[dict[str, Any]]) -> str:
    """Detect which provider returned the odds data based on event shape."""
    if not odds:
        return "unknown"
    sample = odds[0]
    has_sharp_keys = any(
        k in sample for k in ("id", "sport_key")
    ) and "bookmakers" in sample
    if has_sharp_keys:
        return "sharpapi"
    return "odds_api"


def _attach_unavailable_odds(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from api.sharp_odds_client import build_game_market_context
    for game in slate:
        game.update({"odds_event_id": None, "best_available_prices": [],
                     "bookmakers": [], "odds_status": UNAVAILABLE})
        game["market_context"] = build_game_market_context(game, [], "unknown")
    return slate


def _market_context_from_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact market summary stored on each game for admin diagnostics."""
    context = {"moneyline": [], "runline": [], "total": [], "team_totals": [], "odds_found": bool(prices)}
    for price in prices:
        market = price.get("market")
        label = price.get("description") or price.get("outcome")
        point = price.get("point")
        american = price.get("price")
        entry = {"label": label, "odds": american}
        if market == "h2h":
            context["moneyline"].append(entry)
        elif market == "spreads":
            context["runline"].append({**entry, "line": point})
        elif market == "totals":
            context["total"].append({**entry, "line": point})
        elif market == "team_totals":
            context["team_totals"].append({**entry, "line": point})
    return context


def _build_sharp_url(sport_param: str, league: str | None, event_date: str | None, markets: str, *, sportsbook: str | None = None, endpoint: str | None = None) -> str:
    """Build a sanitized Sharp API request URL for debug display.

    No API key in URL — Sharp uses X-API-Key header for auth.
    ``endpoint``: ``/odds`` (default), ``/odds/best``, or ``/events``.
    """
    from api.sharp_odds_client import SHARP_ENDPOINT_ODDS, _base_url as sharp_base
    base = sharp_base()
    ep = endpoint or SHARP_ENDPOINT_ODDS
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    if sportsbook:
        params["sportsbook"] = sportsbook
    if ep == SHARP_ENDPOINT_ODDS and markets:
        params["market"] = markets
    if event_date:
        params["commenceTimeFrom"], params["commenceTimeTo"] = mlb_utc_query_window(event_date)
    import urllib.parse
    return f"{base}{ep}?{urllib.parse.urlencode(params, safe=',')}"


def _build_odds_api_url(event_date: str | None = None) -> str:
    """Build a sanitized The Odds API request URL for debug display."""
    params: dict[str, str] = {
        "apiKey": "REDACTED",
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if event_date:
        params["commenceTimeFrom"] = f"{event_date}T00:00:00Z"
        params["commenceTimeTo"] = f"{event_date}T23:59:59Z"
    import urllib.parse
    return f"https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/?{urllib.parse.urlencode(params)}"


def odds_debug_payload(
    odds_api_key: str,
    selected_date: str,
    sport: str = "mlb",
    league: str | None = None,
    event_date: str | None = None,
    include_started: bool = False,
    parsed_flags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner-only diagnostics for odds providers by sport."""
    from api.sharp_odds_client import ALL_SPORTSBOOKS, MLB_MAPPINGS, SHARP_ENDPOINT_BEST_ODDS, SHARP_ENDPOINT_ODDS, SHARP_SPORT_MAP, _active_endpoint, _use_best_odds, default_sportsbook, game_market_diagnostic, health as sharp_health, prop_market_diagnostic, secondary_sportsbook, sharp_api_enabled, sharp_api_key
    from services.odds_provider_router import provider_status as router_status

    sport_lower = sport.lower()
    cfg = SHARP_SPORT_MAP.get(sport_lower, {})
    markets_requested = "moneyline,spread,total,team_total,f5_moneyline" if sport_lower == "mlb" else cfg.get("markets", "h2h,spreads,totals")
    if league and league.startswith("--"):
        league = None
    sharp_league = "mlb" if sport_lower == "mlb" else (league or cfg.get("league"))

    default_sb = default_sportsbook()
    secondary_sb = secondary_sportsbook()
    active_ep = SHARP_ENDPOINT_ODDS if sport_lower == "mlb" else _active_endpoint()
    use_best = _use_best_odds()
    payload: dict[str, Any] = {
        "sport": sport_lower,
        "league": league,
        "parsed_sport": sport_lower,
        "parsed_league": league,
        "parsed_flags": list(parsed_flags or []),
        "include_started": bool(include_started),
        "sharp_league": sharp_league,
        "event_date": event_date or selected_date,
        "odds_api_key_loaded": bool(str(odds_api_key or "").strip()),
        "odds_api_status_code": None,
        "sharp_api_enabled": sharp_api_enabled(),
        "sharp_api_key_loaded": bool(sharp_api_key()),
        "sharp_api_health": sharp_health(sport=sport_lower),
        "sport_key": cfg.get("cache_key", "baseball_mlb"),
        "markets_requested": markets_requested,
        "provider": None,
        "games_returned": 0,
        "matched_to_mlb_game_pk": 0,
        "unmatched_odds_games": [],
        "unmatched_mlb_games": [],
        "last_error": "",
        "errors": [],
        "provider_status": router_status(sport=sport_lower),
        "sharp_request_url": None,
        "odds_api_request_url": None,
        "sharp_mlb_probes": None,
        "default_sportsbook": default_sb,
        "secondary_sportsbook": secondary_sb,
        "active_sportsbook": None,
        "endpoint_used": active_ep,
        "use_best_odds": use_best,
        "auth_method": "X-API-Key header only",
        "sportsbook_game_counts": {},
        "events_returned": 0,
        "market_context_available": False,
        "first_matched_game": None,
        "sharp_raw_rows": 0,
        "accepted_game_market_rows": 0,
        "rejected_prop_rows": 0,
        "accepted_prop_rows": 0,
        "moneyline_contexts": 0,
        "runline_contexts": 0,
        "total_contexts": 0,
        "team_total_contexts": 0,
        "game_market_sportsbook": "draftkings",
        "prop_sportsbook": "fanduel",
        "requested_card_date_et": event_date or selected_date,
        "utc_query_window": list(mlb_utc_query_window(event_date or selected_date)) if sport_lower == "mlb" else None,
        "sharp_local_date_rows": 0,
        "mlb_local_date_games": 0,
        "team_name_matches": 0,
        "time_window_rejections": 0,
        "date_rejections": 0,
        "missing_team_rejections": 0,
        "doubleheader_closest_time_matches": 0,
        "unmatched_mapping_diagnostics": [],
    }

    schedule: list[dict[str, Any]] = []
    if sport_lower == "mlb":
        payload["mlb_schedule_games"] = 0
        try:
            schedule = get_mlb_schedule(selected_date)
            payload["mlb_schedule_games"] = len(schedule)
            local_schedule = [game for game in schedule if mlb_local_game_date(game.get("game_time")) == selected_date]
            payload["mlb_local_date_games"] = len(local_schedule)
            schedule = local_schedule
        except Exception as error:
            payload["errors"].append(f"MLB schedule unavailable: {error}")

    # Build sanitized request URLs for display
    if sport_lower == "mlb":
        payload["odds_api_request_url"] = _build_odds_api_url(event_date=event_date or selected_date)
        payload["sharp_mlb_probes"] = []
        # Best-odds probe
        payload["sharp_mlb_probes"].append({
            "url": _build_sharp_url("baseball", "mlb", event_date or selected_date, markets_requested, sportsbook="draftkings", endpoint=SHARP_ENDPOINT_ODDS),
            "endpoint": SHARP_ENDPOINT_ODDS,
            "sport_param": "baseball",
            "league": "mlb",
            "sportsbook": "draftkings",
            "status_code": None,
            "games_count": 0,
            "error": None,
        })
        for m in MLB_MAPPINGS:
            sp = m["sport_param"]
            lg = "mlb"
            sb = m.get("sportsbook")
            payload["sharp_mlb_probes"].append({
                "url": _build_sharp_url(sp, lg, event_date or selected_date, markets_requested, sportsbook=sb, endpoint=SHARP_ENDPOINT_ODDS),
                "endpoint": SHARP_ENDPOINT_ODDS,
                "sport_param": sp,
                "league": lg,
                "sportsbook": sb,
                "status_code": None,
                "games_count": 0,
                "error": None,
            })
        cfg_map = cfg
        payload["sharp_request_url"] = _build_sharp_url(
            cfg_map.get("sport_param", "baseball"),
            "mlb",
            event_date or selected_date,
            markets_requested,
            sportsbook=default_sb,
            endpoint=SHARP_ENDPOINT_ODDS,
        )
    else:
        cfg_map = cfg
        payload["sharp_request_url"] = _build_sharp_url(
            cfg_map.get("sport_param", "baseball"),
            sharp_league,
            event_date or selected_date,
            markets_requested,
            endpoint=SHARP_ENDPOINT_ODDS,
        )
        payload["odds_api_request_url"] = "N/A (sport not supported by The Odds API)"

    odds: list[dict[str, Any]] = []
    try:
        from services.odds_provider_router import fetch_odds
        kwargs: dict[str, Any] = {"sport": sport_lower}
        if league:
            kwargs["league"] = league
        if event_date or sport_lower == "mlb":
            kwargs["event_date"] = event_date or selected_date
        odds = fetch_odds(**kwargs)
        payload["provider"] = _detect_odds_provider(odds)
        payload["odds_api_status_code"] = 200
    except Exception as error:
        if not odds and odds_api_key and sport_lower == "mlb":
            try:
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
                if isinstance(decoded, list):
                    odds = decoded
                    payload["provider"] = "odds_api"
            except Exception as fallback_error:
                redacted = _redact_secret(str(fallback_error))
                payload["last_error"] = redacted
                payload["errors"].append(f"Odds API fallback: {redacted}")

    # Reuse diagnostics from the isolated DraftKings game-market fetch. Never
    # issue an unfiltered probe here because that can return player props.
    if sport_lower == "mlb" and isinstance(payload.get("sharp_mlb_probes"), list):
        diagnostic = game_market_diagnostic()
        prop_diagnostic = prop_market_diagnostic()
        payload["sharp_mlb_probes"] = diagnostic.get("attempts") or []
        payload["sharp_raw_rows"] = int(diagnostic.get("raw_rows") or 0)
        payload["accepted_game_market_rows"] = int(diagnostic.get("accepted_game_market_rows") or 0)
        payload["rejected_prop_rows"] = int(diagnostic.get("rejected_prop_rows") or 0)
        payload["sharp_local_date_rows"] = int(diagnostic.get("sharp_local_date_rows") or 0)
        payload["date_rejections"] = sum(int(attempt.get("rejected_wrong_local_date") or 0) for attempt in diagnostic.get("attempts") or [])
        payload["missing_team_rejections"] = int(diagnostic.get("rejected_missing_team_fields") or 0)
        payload["utc_query_window"] = diagnostic.get("utc_query_window") or payload["utc_query_window"]
        payload["accepted_prop_rows"] = int(prop_diagnostic.get("accepted_prop_rows") or 0)
        counts = diagnostic.get("market_counts") or {}
        payload["moneyline_contexts"] = int(counts.get("moneyline") or 0)
        payload["runline_contexts"] = int(counts.get("runline") or 0)
        payload["total_contexts"] = int(counts.get("total") or 0)
        payload["team_total_contexts"] = int(counts.get("team_total") or 0)
        payload["events_returned"] = int(diagnostic.get("events_returned") or 0)
        payload["active_sportsbook"] = diagnostic.get("sportsbook") or "draftkings"
        if diagnostic.get("error"):
            payload["errors"].append(str(diagnostic["error"]))

    if not odds:
        payload["errors"].append("No odds data returned from any provider.")
    payload["games_returned"] = len(odds)
    # Determine active sportsbook from provider + probe data
    if odds and sport_lower == "mlb":
        sb_counts = payload.get("sportsbook_game_counts", {})
        best_sb = max(sb_counts, key=sb_counts.get) if sb_counts else default_sb
        payload["active_sportsbook"] = best_sb if sb_counts.get(best_sb, 0) >= payload["games_returned"] else default_sb
        payload["sharp_request_url"] = _build_sharp_url(
            cfg.get("sport_param", "baseball"),
            "mlb",
            event_date or selected_date,
            markets_requested,
            sportsbook=payload["active_sportsbook"],
            endpoint=SHARP_ENDPOINT_ODDS,
        )
    if schedule:
        odds_keys: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for odds_game in odds:
            if mlb_local_game_date(odds_game.get("commence_time")) != selected_date:
                continue
            odds_keys.setdefault(_game_key(odds_game.get("away_team", ""), odds_game.get("home_team", "")), []).append(odds_game)
        matched_odds_ids: set[int] = set()
        for game in schedule:
            key = _game_key(game.get("away_team", ""), game.get("home_team", ""))
            candidates = odds_keys.get(key, [])
            candidate_count = len(candidates)
            if candidates:
                payload["team_name_matches"] += 1
            match, difference_minutes, rejection = _closest_odds_game_detail(game, candidates)
            if match:
                matched_odds_ids.add(id(match))
                payload["matched_to_mlb_game_pk"] += 1
                if candidate_count > 1:
                    payload["doubleheader_closest_time_matches"] += 1
                if payload["first_matched_game"] is None:
                    payload["first_matched_game"] = f"{game.get('away_team')} @ {game.get('home_team')}"
            else:
                if rejection == "time_window_mismatch":
                    payload["time_window_rejections"] += 1
                payload["unmatched_mlb_games"].append({
                    "game_pk": game.get("game_pk"),
                    "away_team": game.get("away_team"),
                    "home_team": game.get("home_team"),
                    "normalized_key": _game_key_text(game.get("away_team", ""), game.get("home_team", "")),
                    "rejection_reason": rejection,
                    "time_difference_minutes": difference_minutes,
                })
        for key, odds_games in odds_keys.items():
            for odds_game in odds_games:
                if id(odds_game) in matched_odds_ids:
                    continue
                payload["unmatched_odds_games"].append({
                    "id": odds_game.get("id"),
                    "away_team": odds_game.get("away_team"),
                    "home_team": odds_game.get("home_team"),
                    "commence_time": odds_game.get("commence_time"),
                    "normalized_key": _game_key_text(odds_game.get("away_team", ""), odds_game.get("home_team", "")),
                })
                sharp_time = _parse_api_time(odds_game.get("commence_time"))
                exact_mlb = [game for game in schedule if _game_key(game.get("away_team", ""), game.get("home_team", "")) == key]
                reversed_mlb = [game for game in schedule if _game_key(game.get("home_team", ""), game.get("away_team", "")) == key]
                pool = exact_mlb or reversed_mlb or schedule
                closest = min(
                    pool,
                    key=lambda game: abs((_parse_api_time(game.get("game_time")) - sharp_time).total_seconds())
                    if sharp_time and _parse_api_time(game.get("game_time")) else float("inf"),
                ) if pool else None
                closest_time = _parse_api_time(closest.get("game_time")) if closest else None
                diff = abs((closest_time - sharp_time).total_seconds()) / 60.0 if closest_time and sharp_time else None
                reason = "reversed_home_away_diagnostic" if reversed_mlb and not exact_mlb else "time_window_mismatch" if exact_mlb and diff is not None and diff > 720 else "team_name_mismatch"
                local_start = to_et(sharp_time)
                payload["unmatched_mapping_diagnostics"].append({
                    "sharp_away": odds_game.get("away_team"), "sharp_home": odds_game.get("home_team"),
                    "sharp_normalized_away": key[0], "sharp_normalized_home": key[1],
                    "sharp_local_start": local_start.isoformat() if local_start else None,
                    "mlb_away": closest.get("away_team") if closest else None,
                    "mlb_home": closest.get("home_team") if closest else None,
                    "mlb_normalized_away": _normalize_team(closest.get("away_team", "")) if closest else None,
                    "mlb_normalized_home": _normalize_team(closest.get("home_team", "")) if closest else None,
                    "time_difference_minutes": round(diff, 1) if diff is not None else None,
                    "rejection_reason": reason,
                })
    if payload.get("accepted_game_market_rows"):
        payload["provider"] = "sharpapi"
        payload["endpoint_used"] = "/odds"
    payload["market_context_available"] = bool(payload.get("accepted_game_market_rows") and payload.get("matched_to_mlb_game_pk"))
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
        from services.odds_provider_router import fetch_odds, enrich_slate_market_context
        odds = fetch_odds(event_date=selected_date)
        if odds:
            provider = _detect_odds_provider(odds)
            slate = combine_schedule_and_odds(slate, odds, selected_date)
            slate = enrich_slate_market_context(slate, odds, provider)
        else:
            raise MLBDataError("Odds provider router returned empty")
    except Exception as error:
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
