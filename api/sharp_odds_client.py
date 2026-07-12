"""Sharp API — multi-sport odds provider with MarketContext support.

Extends api.sharp_client with sport-agnostic odds fetching and the
standard market_context format.

market_context dict format:
  provider          — "sharp_api" or "odds_api"
  sport             — str
  league            — str | None
  odds_found        — bool
  moneyline         — list[dict]  (label, odds)
  spread_or_runline — list[dict]  (label, line, odds)
  total             — list[dict]  (label, line, odds)
  team_totals       — list[dict]  (label, line, odds)
  player_props      — list[dict]  (label, odds)
  last_updated      — str | None
  matched_event     — int | str | None
  matched_by        — str
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from api.sharp_client import (
    SharpAPIError,
    SharpRateLimitError,
    SHARP_CACHE_TTL,
    _cache_path,
    _cache_read,
    _cache_write,
    _normalize_sharp_event,
    _throttle,
    build_market_context as _build_market_context_obj,
    clear_cache,
    fetch_mlb_odds,
    odds_api_backup_only,
    odds_api_enabled,
    parse_sharp_response,
    sharp_api_enabled,
    sharp_api_key,
)
from core.market import MarketContext
from time_utils import mlb_local_game_date, mlb_utc_query_window

logger = logging.getLogger(__name__)

GAME_MARKET_BOOK = os.getenv("GAME_MARKET_BOOK", "draftkings").strip().lower() or "draftkings"
PROP_MARKET_BOOK = os.getenv("PROP_BOOK", "fanduel").strip().lower() or "fanduel"
ADVANCED_GAME_MARKETS = {
    "moneyline", "money_line", "h2h", "ml", "spread", "runline", "run_line",
    "game_spread", "total", "game_total", "total_runs", "over_under", "team_total",
    "f5_moneyline", "first_5_moneyline", "first_five_moneyline", "f5_ml",
}
PROP_MARKETS = {
    "player_hits", "player_total_bases", "player_home_runs", "pitcher_strikeouts",
    "player_strikeouts", "player_rbis", "player_runs", "player_hits_runs_rbis",
}
_GAME_MARKET_GROUPS = {
    "moneyline": {"moneyline", "money_line", "h2h", "ml"},
    "runline": {"spread", "runline", "run_line", "game_spread"},
    "total": {"total", "game_total", "total_runs", "over_under"},
    "team_total": {"team_total"},
    "f5_moneyline": {"f5_moneyline", "first_5_moneyline", "first_five_moneyline", "f5_ml"},
}
_LAST_GAME_MARKET_DIAGNOSTIC: dict[str, Any] = {}
_LAST_PROP_MARKET_DIAGNOSTIC: dict[str, Any] = {}

# ── Sport mapping ───────────────────────────────────────────────────────────
# Sharp API parameters per sport.
SHARP_SPORT_MAP: dict[str, dict[str, Any]] = {
    "mlb": {
        "sport_param": "baseball",
        "league": "MLB",
        "cache_key": "baseball_mlb",
        "markets": "h2h,spreads,totals,team_totals",
    },
    "soccer": {
        "sport_param": "soccer",
        "league": None,
        "cache_key": "soccer",
        "markets": "h2h,spreads,totals",
    },
    "nba": {
        "sport_param": "basketball",
        "league": "NBA",
        "cache_key": "basketball_nba",
        "markets": "h2h,spreads,totals",
    },
    "nfl": {
        "sport_param": "football",
        "league": "NFL",
        "cache_key": "football_nfl",
        "markets": "h2h,spreads,totals",
    },
    "nhl": {
        "sport_param": "hockey",
        "league": "NHL",
        "cache_key": "hockey_nhl",
        "markets": "h2h,spreads,totals",
    },
}

# Supported soccer leagues for league-scoped requests.
SOCCER_LEAGUES: set[str] = {
    "epl", "la liga", "bundesliga", "serie a", "mls", "liga mx",
}

__all__ = [
    "ALL_SPORTSBOOKS",
    "SharpAPIError",
    "SharpRateLimitError",
    "MarketContext",
    "MLB_MAPPINGS",
    "SHARP_SPORT_MAP",
    "SHARP_ENDPOINT_BEST_ODDS",
    "SHARP_ENDPOINT_EVENTS",
    "SHARP_ENDPOINT_ODDS",
    "SOCCER_LEAGUES",
    "build_game_market_context",
    "build_sharp_odds_url",
    "clear_cache",
    "default_sportsbook",
    "fetch_mlb_odds",
    "get_odds",
    "get_soccer_odds",
    "fetch_mlb_game_markets",
    "fetch_mlb_props",
    "game_market_diagnostic",
    "prop_market_diagnostic",
    "ADVANCED_GAME_MARKETS",
    "GAME_MARKET_BOOK",
    "PROP_MARKET_BOOK",
    "probe_sharp_mlb",
    "health",
    "odds_api_backup_only",
    "odds_api_enabled",
    "sharp_api_enabled",
    "sharp_api_key",
    "_active_endpoint",
    "_use_best_odds",
]


def build_game_market_context(
    game: dict[str, Any],
    prices: list[dict[str, Any]],
    provider: str,
    *,
    sport: str = "mlb",
    league: str | None = None,
    last_updated: str | None = None,
) -> dict[str, Any]:
    """Build a market_context dict in the standard format.

    This dict is attached to each game row in the slate.  It is the
    single source of truth for market-context validation.
    """
    ctx = _market_context_from_prices(prices)
    matched_event = game.get("game_pk") or game.get("game_id") or game.get("id")
    try:
        matched_event = int(matched_event) if matched_event is not None else None
    except (TypeError, ValueError):
        matched_event = str(matched_event) if matched_event is not None else None
    home = str(game.get("home_team") or "")
    away = str(game.get("away_team") or "")
    def _team_price(rows: list[dict[str, Any]], team: str) -> Any:
        target = _normalize_team_name(team)
        return next((row.get("odds") for row in rows if _normalize_team_name(str(row.get("label") or "")) == target), None)
    def _team_spread(rows: list[dict[str, Any]], team: str) -> Any:
        target = _normalize_team_name(team)
        return next((row.get("line") for row in rows if _normalize_team_name(str(row.get("label") or "")) == target), None)
    def _total_line(label: str) -> Any:
        return next((row.get("line") for row in ctx.get("total", []) if str(row.get("label") or "").lower().startswith(label)), None)
    def _team_total_line(team: str) -> Any:
        target = _normalize_team_name(team)
        return next((row.get("line") for row in ctx.get("team_totals", []) if target in _normalize_team_name(str(row.get("label") or ""))), None)
    sportsbook = next((price.get("bookmaker_key") or price.get("bookmaker") for price in prices if price.get("bookmaker_key") or price.get("bookmaker")), None)
    normalized_provider = "sharpapi" if provider in {"sharp_api", "sharpapi"} else provider
    return {
        "provider": normalized_provider,
        "sport": sport,
        "league": league or "",
        "odds_found": bool(prices),
        "moneyline": ctx.get("moneyline", []),
        "spread_or_runline": ctx.get("spread_or_runline", []),
        "total": ctx.get("total", []),
        "team_totals": ctx.get("team_totals", []),
        "player_props": ctx.get("player_props", []),
        "last_updated": last_updated,
        "matched_event": matched_event,
        "matched_by": provider,
        "game_id": matched_event,
        "game_pk": matched_event,
        "away_team": away,
        "home_team": home,
        "start_time": game.get("game_time") or game.get("start_time"),
        "moneyline_home": _team_price(ctx.get("moneyline", []), home),
        "moneyline_away": _team_price(ctx.get("moneyline", []), away),
        "spread_home": _team_spread(ctx.get("spread_or_runline", []), home),
        "spread_away": _team_spread(ctx.get("spread_or_runline", []), away),
        "total_over": _total_line("over"),
        "total_under": _total_line("under"),
        "team_total_home": _team_total_line(home),
        "team_total_away": _team_total_line(away),
        "sportsbook": sportsbook,
        "line_verified": bool(prices),
        "accepted_game_market_rows": len(prices),
        "matched_games": 1 if prices else 0,
        "market_context_available": bool(prices),
    }


def _normalize_team_name(value: str) -> str:
    from api.sharp_client import _normalize_team
    return _normalize_team(value)


def _market_context_from_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert best_available_prices to the standard market_context format."""
    context: dict[str, Any] = {
        "moneyline": [], "spread_or_runline": [], "total": [],
        "team_totals": [], "player_props": [],
    }
    for price in prices:
        market = price.get("market")
        label = price.get("description") or price.get("outcome")
        point = price.get("point")
        american = price.get("price")
        entry = {"label": label, "odds": american}
        if market == "h2h":
            context["moneyline"].append(entry)
        elif market == "spreads":
            context["spread_or_runline"].append({**entry, "line": point})
        elif market == "totals":
            context["total"].append({**entry, "line": point})
        elif market == "team_totals":
            context["team_totals"].append({**entry, "line": point})
        elif market in ("player_props", "player_prop", "props"):
            context["player_props"].append(entry)
    return context


# ── Sportsbook config ────────────────────────────────────────────────────────


def default_sportsbook() -> str:
    return os.getenv("SHARP_DEFAULT_SPORTSBOOK", "draftkings").strip().lower()


def secondary_sportsbook() -> str:
    return os.getenv("SHARP_SECONDARY_SPORTSBOOK", "fanduel").strip().lower()


# ── Endpoint constants ──────────────────────────────────────────────────────
# Defined before any function that uses them as defaults to avoid NameError
# at import time (Python evaluates default argument values at definition time).
SHARP_ENDPOINT_ODDS = "/odds"
SHARP_ENDPOINT_BEST_ODDS = "/odds/best"
SHARP_ENDPOINT_EVENTS = "/events"


# ── Supported sportsbooks ───────────────────────────────────────────────────
ALL_SPORTSBOOKS: list[str] = [
    "draftkings", "fanduel", "hardrockbet", "betmgm", "caesars", "pinnacle",
]

_LAST_REQUEST_DIAGNOSTIC: dict[str, Any] = {}


# ── MLB mapping fallbacks ───────────────────────────────────────────────────
# Each mapping includes sport_param, optional league, and optional sportsbook.
# The production request tries: best-odds → default sportsbook → secondary → probe all sportsbooks.
MLB_MAPPINGS: list[dict[str, str | None]] = [
    {"sport_param": "baseball", "league": "MLB", "sportsbook": GAME_MARKET_BOOK},
]


def build_sharp_odds_url(
    sport_param: str,
    league: str | None = None,
    event_date: str | None = None,
    *,
    markets: str | None = None,
    sportsbook: str | None = None,
    endpoint: str | None = None,
) -> str:
    """Build the Sharp API request URL for debug display (no key exposed).

    Auth is sent via X-API-Key header, not in the URL.
    ``endpoint`` controls the URL path (``/odds``, ``/odds/best``, ``/events``).
    """
    ep = endpoint or SHARP_ENDPOINT_ODDS
    base = _base_url()
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    if ep == SHARP_ENDPOINT_ODDS:
        params["market"] = markets or "moneyline,spread,total,team_total"
        if sportsbook:
            params["sportsbook"] = sportsbook
    if event_date:
        params["commenceTimeFrom"], params["commenceTimeTo"] = mlb_utc_query_window(event_date)
    import urllib.parse
    return f"{base}{ep}?{urllib.parse.urlencode(params)}"


def _do_sharp_request(
    url: str,
    params: dict[str, str],
    headers: dict[str, str] | None,
) -> requests.Response:
    """Send one Sharp API GET, returning the response or raising."""
    _throttle()
    try:
        response = requests.get(url, params=params, headers=headers, timeout=20)
        sanitized_url = requests.Request("GET", url, params=params).prepare().url or url
        endpoint = "/" + url.rstrip("/").split("/")[-1]
        if url.rstrip("/").endswith("/odds/best"):
            endpoint = "/odds/best"
        logger.info("Sharp request endpoint=%s url=%s status=%s", endpoint, sanitized_url, response.status_code)
        return response
    except requests.RequestException as exc:
        raise SharpAPIError(f"Sharp API request failed: {exc}") from exc


def _use_best_odds() -> bool:
    return os.getenv("SHARP_USE_BEST_ODDS", "true").strip().lower() in {"1", "true", "yes", "on"}


def _sharpen_request(
    sport_param: str,
    league: str | None,
    event_date: str | None,
    api_key: str,
    base_url: str,
    markets: str,
    cache_key: str,
    *,
    sportsbook: str | None = None,
    endpoint: str | None = None,
) -> list[dict[str, Any]]:
    """Make one Sharp API request and return normalized events.

    Uses ``SHARP_USE_BEST_ODDS`` env var:
      - ``true`` (default): calls ``/odds/best`` (no sportsbook filter, all markets).
      - ``false``: calls ``/odds`` with optional sportsbook filter.

    Auth: ``X-API-Key`` header only — no query-param fallback."""
    cached = _cache_read(cache_key)
    if cached is not None:
        logger.debug("Sharp cache HIT for %s", cache_key)
        return cached

    endpoint = endpoint or (SHARP_ENDPOINT_BEST_ODDS if _use_best_odds() else SHARP_ENDPOINT_ODDS)
    url = f"{base_url}{endpoint}"
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    # Only send markets and sportsbook for /odds endpoint, not /odds/best
    if endpoint == SHARP_ENDPOINT_ODDS:
        params["market"] = markets
        if sportsbook:
            params["sportsbook"] = sportsbook
    if event_date:
        params["commenceTimeFrom"], params["commenceTimeTo"] = mlb_utc_query_window(event_date)

    headers = {"X-API-Key": api_key}
    resp = _do_sharp_request(url, params, headers)

    if resp.status_code == 401:
        raise SharpAPIError("auth_failed", status_code=401, code="auth_failed")
    if resp.status_code == 429:
        raise SharpRateLimitError("Sharp API rate limit hit (429)")
    if resp.status_code != 200:
        raise SharpAPIError(f"http_error_{resp.status_code}", status_code=resp.status_code)

    try:
        data = resp.json()
    except Exception as exc:
        raise SharpAPIError("parse_failed", code="parse_failed", response_keys=[]) from exc
    parsed = parse_sharp_response(data, endpoint=endpoint)
    normalized = parsed["odds"] or parsed["events"]
    global _LAST_REQUEST_DIAGNOSTIC
    _LAST_REQUEST_DIAGNOSTIC = {
        "endpoint": endpoint, "http_status": resp.status_code,
        "top_level_keys": parsed["top_level_keys"],
        "events_returned": parsed["events_returned"], "odds_returned": parsed["odds_rows_returned"],
        "error": parsed["error"], "sportsbook": sportsbook,
    }
    logger.info("Sharp response endpoint=%s keys=%s games=%d events=%d", endpoint, parsed["top_level_keys"], len(normalized), parsed["events_returned"])
    _cache_write(cache_key, normalized)
    return normalized


def probe_sharp_mlb(
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Test all MLB mappings: best-odds first, then each sportsbook.

    Returns a list of result dicts, one per mapping:
      {endpoint, sport_param, league, sportsbook, status_code, games_count, error, first_matchup}.
    """
    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")
    base_url = _base_url()
    cfg = SHARP_SPORT_MAP["mlb"]
    sp = cfg["sport_param"]
    lg = "MLB"
    markets = cfg["markets"]

    results: list[dict[str, Any]] = []

    # 1. Best-odds probe (no sportsbook filter)
    for endpoint_label, endpoint_path, sb_val in [
        ("/odds/best", SHARP_ENDPOINT_BEST_ODDS, None),
    ]:
        ck = f"probe_best"
        if event_date:
            ck += f"_{event_date}"
        result: dict[str, Any] = {
            "endpoint": endpoint_label,
            "sport_param": sp,
            "league": lg,
            "sportsbook": sb_val,
            "status_code": None,
            "games_count": 0,
            "error": None,
            "first_matchup": None,
        }
        try:
            odds = _sharpen_request_for_probe(
                sp, lg, event_date, api_key, base_url, markets, ck,
                endpoint=endpoint_path, sportsbook=sb_val,
            )
            result["games_count"] = len(odds)
            result["status_code"] = 200
            if odds:
                first = odds[0]
                result["first_matchup"] = f"{first.get('away_team')} @ {first.get('home_team')} [{first.get('commence_time', '')}]"
                books = first.get("bookmakers") or []
                first_markets = books[0].get("markets") if books else []
                result["first_market"] = first_markets[0].get("key") if first_markets else None
            else:
                result["error"] = "no_events_returned"
            result.update({key: _LAST_REQUEST_DIAGNOSTIC.get(key) for key in ("top_level_keys", "events_returned", "odds_returned")})
        except SharpRateLimitError:
            result["status_code"] = 429
            result["error"] = "rate_limited"
        except SharpAPIError as exc:
            result["status_code"] = getattr(exc, "status_code", None)
            result["error"] = getattr(exc, "code", None) or str(exc)[:200]
            result["top_level_keys"] = getattr(exc, "response_keys", [])
        except Exception as exc:
            result["error"] = str(exc)[:200]
        results.append(result)

    # 2. Each sportsbook via /odds
    for mapping in MLB_MAPPINGS:
        sp_m = mapping["sport_param"]
        lg_m = mapping["league"]
        sb_m = mapping.get("sportsbook")
        ck = f"probe_{sp_m}"
        if lg_m:
            ck += f"_{lg_m.replace(' ', '_')}"
        if sb_m:
            ck += f"_{sb_m}"
        if event_date:
            ck += f"_{event_date}"
        result = {
            "endpoint": "/odds",
            "sport_param": sp_m,
            "league": lg_m,
            "sportsbook": sb_m,
            "status_code": None,
            "games_count": 0,
            "error": None,
            "first_matchup": None,
        }
        try:
            odds = _sharpen_request(sp_m, lg_m, event_date, api_key, base_url, markets, ck, sportsbook=sb_m, endpoint=SHARP_ENDPOINT_ODDS)
            result["games_count"] = len(odds)
            result["status_code"] = 200
            if odds:
                first = odds[0]
                result["first_matchup"] = f"{first.get('away_team')} @ {first.get('home_team')} [{first.get('commence_time', '')}]"
                books = first.get("bookmakers") or []
                first_markets = books[0].get("markets") if books else []
                result["first_market"] = first_markets[0].get("key") if first_markets else None
            else:
                result["error"] = "no_events_returned"
            result.update({key: _LAST_REQUEST_DIAGNOSTIC.get(key) for key in ("top_level_keys", "events_returned", "odds_returned")})
        except SharpRateLimitError:
            result["status_code"] = 429
            result["error"] = "rate_limited"
        except SharpAPIError as exc:
            result["status_code"] = getattr(exc, "status_code", None)
            result["error"] = getattr(exc, "code", None) or str(exc)[:200]
            result["top_level_keys"] = getattr(exc, "response_keys", [])
        except Exception as exc:
            result["error"] = str(exc)[:200]
        results.append(result)
        if result["games_count"] > 0 and not _cache_read("baseball_mlb"):
            _cache_write("baseball_mlb", odds)
    return results


def _sharpen_request_for_probe(
    sport_param: str,
    league: str | None,
    event_date: str | None,
    api_key: str,
    base_url: str,
    markets: str,
    cache_key: str,
    *,
    endpoint: str | None = None,
    sportsbook: str | None = None,
) -> list[dict[str, Any]]:
    """Like _sharpen_request but caller picks the endpoint path."""
    ep = endpoint or SHARP_ENDPOINT_ODDS
    url = f"{base_url}{ep}"
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    if ep == SHARP_ENDPOINT_ODDS:
        params["market"] = markets
        if sportsbook:
            params["sportsbook"] = sportsbook
    if event_date:
        params["commenceTimeFrom"], params["commenceTimeTo"] = mlb_utc_query_window(event_date)

    headers = {"X-API-Key": api_key}
    resp = _do_sharp_request(url, params, headers)

    if resp.status_code == 401:
        raise SharpAPIError("auth_failed", status_code=401, code="auth_failed")
    if resp.status_code == 429:
        raise SharpRateLimitError("Sharp API rate limit hit (429)")
    if resp.status_code != 200:
        raise SharpAPIError(f"http_error_{resp.status_code}", status_code=resp.status_code)

    try:
        data = resp.json()
    except Exception as exc:
        raise SharpAPIError("parse_failed", code="parse_failed", response_keys=[]) from exc
    parsed = parse_sharp_response(data, endpoint=ep)
    normalized = parsed["odds"] or parsed["events"]
    global _LAST_REQUEST_DIAGNOSTIC
    _LAST_REQUEST_DIAGNOSTIC = {
        "endpoint": ep, "http_status": resp.status_code,
        "top_level_keys": parsed["top_level_keys"],
        "events_returned": parsed["events_returned"], "odds_returned": parsed["odds_rows_returned"],
        "error": parsed["error"], "sportsbook": sportsbook,
    }
    logger.info("Sharp response endpoint=%s keys=%s games=%d events=%d", ep, parsed["top_level_keys"], len(normalized), parsed["events_returned"])
    return normalized


def _active_endpoint() -> str:
    """Return the endpoint path currently in use based on SHARP_USE_BEST_ODDS."""
    if _use_best_odds():
        return SHARP_ENDPOINT_BEST_ODDS
    return SHARP_ENDPOINT_ODDS


def _flat_rows(payload: Any) -> tuple[list[dict[str, Any]], list[str]]:
    keys = sorted(str(key) for key in payload.keys()) if isinstance(payload, dict) else ["<list>"] if isinstance(payload, list) else []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = next((payload.get(key) for key in ("data", "odds", "events", "results") if isinstance(payload.get(key), list)), [])
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)], keys


def _market_name(row: dict[str, Any]) -> str:
    return str(row.get("market_type") or row.get("market") or "").strip().lower()


def _market_group(market: str) -> str | None:
    return next((group for group, aliases in _GAME_MARKET_GROUPS.items() if market in aliases), None)


def _filtered_market_request(*, sportsbook: str, markets: str, kind: str, card_date: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")
    endpoint = SHARP_ENDPOINT_ODDS
    url = f"{_base_url()}{endpoint}"
    params = {
        "sport": "baseball", "league": "mlb", "sportsbook": sportsbook,
        "market": markets, "limit": "200",
    }
    utc_window: tuple[str, str] | None = None
    if card_date:
        utc_window = mlb_utc_query_window(card_date)
        params["commenceTimeFrom"], params["commenceTimeTo"] = utc_window
    response = _do_sharp_request(url, params, {"X-API-Key": api_key})
    attempt = {
        "endpoint": endpoint, "http_status": response.status_code,
        "market_requested": markets, "sportsbook": sportsbook,
        "rows_returned": 0, "accepted_rows": 0, "rejected_rows": 0,
        "rejected_prop_rows": 0, "rejected_game_market_rows": 0,
        "rejected_missing_team_fields": 0, "top_level_keys": [], "error": None,
        "first_rejected_prop_market": None,
        "utc_query_window": list(utc_window) if utc_window else None,
        "local_date_rows": 0, "rejected_wrong_local_date": 0,
    }
    if response.status_code == 401:
        attempt["error"] = "auth_failed"
        return [], attempt
    if response.status_code == 429:
        attempt["error"] = "rate_limited"
        return [], attempt
    if response.status_code != 200:
        attempt["error"] = f"http_error_{response.status_code}"
        return [], attempt
    try:
        payload = response.json()
    except Exception:
        attempt["error"] = "parse_failed"
        return [], attempt
    rows, keys = _flat_rows(payload)
    attempt["top_level_keys"] = keys
    attempt["rows_returned"] = len(rows)
    accepted: list[dict[str, Any]] = []
    for row in rows:
        market = _market_name(row)
        start_time = row.get("start_time") or row.get("event_start_time") or row.get("commence_time") or row.get("starts_at") or row.get("scheduled_at")
        if card_date:
            if mlb_local_game_date(start_time) != card_date:
                attempt["rejected_wrong_local_date"] += 1
                continue
            attempt["local_date_rows"] += 1
        has_teams = bool(row.get("home_team") and row.get("away_team"))
        if kind == "game":
            if market in PROP_MARKETS or market.startswith("player_") or market.startswith("pitcher_"):
                attempt["rejected_prop_rows"] += 1
                attempt["first_rejected_prop_market"] = attempt["first_rejected_prop_market"] or market
                continue
            if market not in ADVANCED_GAME_MARKETS:
                attempt["rejected_rows"] += 1
                continue
            if not has_teams:
                attempt["rejected_missing_team_fields"] += 1
                continue
        else:
            if market not in PROP_MARKETS:
                attempt["rejected_game_market_rows"] += 1
                continue
        accepted.append(dict(row))
    attempt["accepted_rows"] = len(accepted)
    logger.info(
        "Sharp filtered endpoint=%s sportsbook=%s market=%s status=%s rows=%d accepted=%d rejected=%d",
        endpoint, sportsbook, markets, response.status_code, len(rows), len(accepted),
        attempt["rejected_rows"] + attempt["rejected_prop_rows"] + attempt["rejected_game_market_rows"] + attempt["rejected_missing_team_fields"],
    )
    return accepted, attempt


def fetch_mlb_game_markets(event_date: str | None = None) -> dict[str, Any]:
    """Fetch only DraftKings MLB game markets for advanced card enrichment."""
    attempts: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    request_sets = [
        "moneyline,spread,total,team_total,f5_moneyline",
        "moneyline,runline,game_total,total_runs,team_total,first_5_moneyline",
        "h2h,money_line,ml",
        "spread,runline,run_line,game_spread",
        "total,game_total,total_runs,over_under",
        "team_total",
    ]
    seen_rows: set[str] = set()
    for markets in request_sets:
        rows, attempt = _filtered_market_request(sportsbook=GAME_MARKET_BOOK, markets=markets, kind="game", card_date=event_date)
        attempts.append(attempt)
        for row in rows:
            signature = json.dumps(row, sort_keys=True, default=str)
            if signature not in seen_rows:
                seen_rows.add(signature)
                accepted.append(row)
        groups = {_market_group(_market_name(row)) for row in accepted}
        if {"moneyline", "runline", "total", "team_total"}.issubset(groups):
            break
    parsed = parse_sharp_response({"data": accepted}, endpoint=SHARP_ENDPOINT_ODDS) if accepted else {"odds": [], "events": [], "events_returned": 0}
    counts = {group: sum(1 for row in accepted if _market_group(_market_name(row)) == group) for group in _GAME_MARKET_GROUPS}
    rejected_props = sum(int(attempt.get("rejected_prop_rows") or 0) for attempt in attempts)
    missing_teams = sum(int(attempt.get("rejected_missing_team_fields") or 0) for attempt in attempts)
    first = accepted[0] if accepted else {}
    result = {
        "provider": "sharpapi", "sportsbook": GAME_MARKET_BOOK,
        "events": parsed.get("events") or parsed.get("odds") or [],
        "odds": parsed.get("odds") or [], "attempts": attempts,
        "raw_rows": sum(int(attempt.get("rows_returned") or 0) for attempt in attempts),
        "accepted_game_market_rows": len(accepted), "rejected_prop_rows": rejected_props,
        "sharp_local_date_rows": sum(int(attempt.get("local_date_rows") or 0) for attempt in attempts),
        "rejected_missing_team_fields": missing_teams, "market_counts": counts,
        "events_returned": int(parsed.get("events_returned") or 0),
        "first_accepted_matchup": f"{first.get('away_team')} @ {first.get('home_team')}" if first else None,
        "first_accepted_market": _market_name(first) if first else None,
        "first_rejected_prop_market": next((market for attempt in attempts for market in [attempt.get("first_rejected_prop_market")] if market), None),
        "status": "available" if accepted else "unavailable",
        "error": None if accepted else "draftkings_game_markets_unavailable",
        "requested_card_date_et": event_date,
        "utc_query_window": list(mlb_utc_query_window(event_date)) if event_date else None,
    }
    global _LAST_GAME_MARKET_DIAGNOSTIC
    _LAST_GAME_MARKET_DIAGNOSTIC = result
    if result["odds"]:
        _cache_write("baseball_mlb_game_markets", result["odds"])
    return result


def fetch_mlb_props(event_date: str | None = None) -> dict[str, Any]:
    """Fetch only FanDuel player props; never feed these rows to game context."""
    markets = (
        "player_hits,player_total_bases,player_home_runs,player_rbis,player_runs,"
        "player_hits_runs_rbis,pitcher_strikeouts,player_strikeouts"
    )
    rows, attempt = _filtered_market_request(sportsbook=PROP_MARKET_BOOK, markets=markets, kind="prop", card_date=event_date)
    counts = {market: sum(1 for row in rows if _market_name(row) == market) for market in PROP_MARKETS}
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    import re
    for row in rows:
        market = _market_name(row)
        raw_selection = str(row.get("selection") or row.get("outcome") or "").strip()
        player = str(row.get("player_name") or row.get("player") or row.get("participant") or "").strip()
        if not player:
            player = re.sub(r"(?i)\b(over|under)\b.*$", "", raw_selection).strip(" -")
        line_raw = row.get("line") if row.get("line") is not None else row.get("point")
        try:
            line = float(line_raw)
        except (TypeError, ValueError):
            line_match = re.search(r"(\d+(?:\.\d+)?)", raw_selection)
            line = float(line_match.group(1)) if line_match else None
        direction_text = str(row.get("side") or row.get("over_under") or row.get("direction") or raw_selection).lower()
        direction = "over" if "over" in direction_text else "under" if "under" in direction_text else ""
        home = str(row.get("home_team") or "")
        away = str(row.get("away_team") or "")
        team = str(row.get("team") or row.get("player_team") or row.get("participant_team") or "")
        opponent = home if team and _normalize_team_name(team) == _normalize_team_name(away) else away if team else ""
        start = row.get("start_time") or row.get("event_start_time") or row.get("commence_time") or row.get("starts_at")
        event_id = str(row.get("event_id") or row.get("game_id") or f"{away}@{home}@{start}")
        key = (event_id, re.sub(r"[^a-z0-9]", "", player.lower()), market, str(line))
        prop = grouped.setdefault(key, {
            "provider": "sharpapi", "sportsbook": PROP_MARKET_BOOK,
            "market_type": market, "player_name": player, "team": team,
            "opponent": opponent, "game_id": row.get("game_id") or row.get("event_id") or row.get("id") or event_id,
            "home_team": home, "away_team": away, "selection": "Over",
            "line": line, "over_odds": None, "under_odds": None,
            "odds_american": None, "start_time": start, "line_verified": line is not None,
            "source": "sharpapi_fanduel_props",
        })
        price = row.get("odds_american") if row.get("odds_american") is not None else row.get("odds")
        if direction == "over":
            prop["over_odds"] = price
            prop["odds_american"] = price
        elif direction == "under":
            prop["under_odds"] = price
        elif prop["odds_american"] is None:
            prop["odds_american"] = price
    grouped_props = list(grouped.values())
    result = {
        "provider": "sharpapi", "sportsbook": PROP_MARKET_BOOK, "rows": rows,
        "grouped_props": grouped_props,
        "attempts": [attempt], "raw_rows": attempt["rows_returned"],
        "accepted_prop_rows": len(rows), "rejected_game_market_rows": attempt["rejected_game_market_rows"],
        "market_counts": counts, "status": "available" if rows else "unavailable",
        "error": None if rows else "fanduel_props_unavailable",
        "first_prop_row": rows[0] if rows else None,
        "first_grouped_prop": grouped_props[0] if grouped_props else None,
    }
    global _LAST_PROP_MARKET_DIAGNOSTIC
    _LAST_PROP_MARKET_DIAGNOSTIC = result
    return result


def game_market_diagnostic() -> dict[str, Any]:
    return dict(_LAST_GAME_MARKET_DIAGNOSTIC)


def prop_market_diagnostic() -> dict[str, Any]:
    return dict(_LAST_PROP_MARKET_DIAGNOSTIC)


def get_odds(
    sport: str = "mlb",
    league: str | None = None,
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch odds for any supported sport from the Sharp API.

    Flow (MLB):
      1. If ``SHARP_USE_BEST_ODDS=true`` (default), call ``/odds/best``
         (covers all sportsbooks).  If it returns games, done.
      2. Fallback: try ``/odds`` with default sportsbook, then secondary,
         then probe all known sportsbooks.

    Non-MLB sports use ``/odds`` with the mapped sport_param.

    Args:
        sport: One of ``mlb``, ``soccer``, ``nba``, ``nfl``, ``nhl``.
        league: League override (e.g. ``"MLB"``).  Falls back to
                ``SHARP_SPORT_MAP[sport]["league"]``.
        event_date: Optional ISO date to scope the request.

    Returns normalized events in The Odds API shape.

    Raises SharpAPIError on connectivity / non-429 failures.
    Raises SharpRateLimitError on 429.
    """
    sport_lower = sport.lower()
    if sport_lower not in SHARP_SPORT_MAP:
        raise SharpAPIError(f"Unsupported sport: {sport}. Supported: {list(SHARP_SPORT_MAP.keys())}")

    cfg = SHARP_SPORT_MAP[sport_lower]
    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")
    base_url = _base_url()

    # ── MLB: isolated DraftKings game markets only ────────────────────────
    if sport_lower == "mlb":
        result = fetch_mlb_game_markets(event_date=event_date)
        return list(result.get("odds") or [])

    # ── Non-MLB sports ────────────────────────────────────────────────────
    cache_key = cfg["cache_key"]
    if league:
        cache_key = f"{cache_key}_{league}"
    if event_date:
        cache_key = f"{cache_key}_{event_date}"

    cached = _cache_read(cache_key)
    if cached is not None:
        logger.debug("Sharp cache HIT for %s", cache_key)
        return cached

    try:
        odds = _sharpen_request(
            cfg["sport_param"], league or cfg["league"], event_date,
            api_key, base_url, cfg["markets"], cache_key,
            endpoint=SHARP_ENDPOINT_ODDS,
        )
    except (SharpAPIError, SharpRateLimitError):
        logger.warning("Sharp primary mapping failed for %s", sport_lower)
        odds = []

    logger.info("Sharp API fetched %d odds events for %s", len(odds), sport_lower)
    return odds


def get_soccer_odds(league: str | None = None, event_date: str | None = None) -> list[dict[str, Any]]:
    """Fetch soccer odds, optionally scoped to a league and date.

    Args:
        league: Optional league name, e.g. ``"EPL"``, ``"La Liga"``,
                ``"MLS"``.  If ``None``, fetches all soccer odds.
        event_date: Optional ISO date to scope the request.

    Returns normalized events in The Odds API shape.
    """
    if league:
        league_clean = league.strip()
        return get_odds(sport="soccer", league=league_clean, event_date=event_date)
    return get_odds(sport="soccer", league=None, event_date=event_date)


def _base_url() -> str:
    """Return the Sharp API base URL from env or default."""
    return os.getenv("SHARP_API_BASE_URL", "https://api.sharpapi.io/api/v1").rstrip("/")


def health(sport: str = "mlb") -> dict[str, Any]:
    """Return Sharp API health status for one sport."""
    api_key_present = bool(sharp_api_key())
    sport_lower = sport.lower()
    cfg = SHARP_SPORT_MAP.get(sport_lower)
    cache_key = cfg["cache_key"] if cfg else "baseball_mlb"
    cache_path = _cache_path(cache_key)
    cache_age: float | None = None
    if cache_path.exists():
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache_age = time.time() - payload.get("cached_at", 0)
        except Exception:
            pass
    return {
        "enabled": sharp_api_enabled(),
        "api_key_loaded": api_key_present,
        "base_url": _base_url(),
        "sport": sport_lower,
        "cache_path": str(cache_path),
        "cache_age_seconds": cache_age,
        "cache_fresh": cache_age is not None and cache_age < SHARP_CACHE_TTL,
        "last_request": dict(_LAST_REQUEST_DIAGNOSTIC),
        "game_markets": dict(_LAST_GAME_MARKET_DIAGNOSTIC),
        "props": dict(_LAST_PROP_MARKET_DIAGNOSTIC),
    }
