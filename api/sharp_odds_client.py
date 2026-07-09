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
    sharp_api_enabled,
    sharp_api_key,
)
from core.market import MarketContext

logger = logging.getLogger(__name__)

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
    "SharpAPIError",
    "SharpRateLimitError",
    "MarketContext",
    "MLB_MAPPINGS",
    "SHARP_SPORT_MAP",
    "SOCCER_LEAGUES",
    "build_game_market_context",
    "clear_cache",
    "default_sportsbook",
    "fetch_mlb_odds",
    "get_odds",
    "get_soccer_odds",
    "probe_sharp_mlb",
    "health",
    "odds_api_backup_only",
    "odds_api_enabled",
    "sharp_api_enabled",
    "sharp_api_key",
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
    return {
        "provider": provider,
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
    }


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


# ── MLB mapping fallbacks ───────────────────────────────────────────────────
# Each mapping includes sport_param, optional league, and optional sportsbook.
# The production request tries: default sportsbook → secondary → no sportsbook.
MLB_MAPPINGS: list[dict[str, str | None]] = [
    {"sport_param": "baseball", "league": "MLB", "sportsbook": "draftkings"},
    {"sport_param": "baseball", "league": "MLB", "sportsbook": "fanduel"},
    {"sport_param": "baseball", "league": "MLB", "sportsbook": None},
    {"sport_param": "mlb", "league": None, "sportsbook": None},
    {"sport_param": "baseball", "league": None, "sportsbook": None},
    {"sport_param": "baseball", "league": "Major League Baseball", "sportsbook": None},
]


def build_sharp_odds_url(
    sport_param: str,
    league: str | None = None,
    event_date: str | None = None,
    *,
    markets: str | None = None,
    sportsbook: str | None = None,
) -> str:
    """Build the Sharp API request URL for debug display (no key exposed).

    Auth is sent via X-API-Key header, not in the URL.
    """
    base = _base_url()
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "markets": markets or "h2h,spreads,totals,team_totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    if sportsbook:
        params["sportsbook"] = sportsbook
    if event_date:
        params["commenceTimeFrom"] = f"{event_date}T00:00:00Z"
        params["commenceTimeTo"] = f"{event_date}T23:59:59Z"
    import urllib.parse
    return f"{base}/odds?{urllib.parse.urlencode(params)}"


def _do_sharp_request(
    url: str,
    params: dict[str, str],
    headers: dict[str, str] | None,
) -> requests.Response:
    """Send one Sharp API GET, returning the response or raising."""
    _throttle()
    try:
        return requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise SharpAPIError(f"Sharp API request failed: {exc}") from exc


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
) -> list[dict[str, Any]]:
    """Make one Sharp API request and return normalized events.

    Auth: X-API-Key header (primary).  If Sharp responds 401 with
    ``missing_api_key``, falls back once to ``api_key`` query param."""
    cached = _cache_read(cache_key)
    if cached is not None:
        logger.debug("Sharp cache HIT for %s", cache_key)
        return cached

    url = f"{base_url}/odds"
    params: dict[str, str] = {
        "sport": sport_param,
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if league:
        params["league"] = league
    if sportsbook:
        params["sportsbook"] = sportsbook
    if event_date:
        params["commenceTimeFrom"] = f"{event_date}T00:00:00Z"
        params["commenceTimeTo"] = f"{event_date}T23:59:59Z"

    # Primary: X-API-Key header
    headers = {"X-API-Key": api_key}
    resp = _do_sharp_request(url, params, headers)

    # Fallback: if 401 missing_api_key, try api_key query param
    if resp.status_code == 401 and "missing_api_key" in (resp.text or "").lower():
        logger.warning("Sharp X-API-Key header rejected (401) — falling back to api_key query param")
        params["api_key"] = api_key
        headers = None
        resp = _do_sharp_request(url, params, headers)

    if resp.status_code == 429:
        raise SharpRateLimitError("Sharp API rate limit hit (429)")
    if resp.status_code != 200:
        raise SharpAPIError(
            f"Sharp API returned HTTP {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
    except Exception as exc:
        raise SharpAPIError(f"Sharp API invalid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise SharpAPIError(f"Sharp API unexpected response type: {type(data).__name__}")

    from api.sharp_client import _normalize_sharp_event
    normalized = [_normalize_sharp_event(event) for event in data]
    _cache_write(cache_key, normalized)
    return normalized


def probe_sharp_mlb(
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Test all MLB sport/league/sportsbook mappings in order.

    Returns a list of result dicts, one per mapping:
      {sport_param, league, sportsbook, status_code, games_count, error, first_matchup}.
    The first mapping that returns games is saved to cache as 'baseball_mlb'.
    """
    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")
    base_url = _base_url()
    cfg = SHARP_SPORT_MAP["mlb"]
    markets = cfg["markets"]

    results: list[dict[str, Any]] = []
    for mapping in MLB_MAPPINGS:
        sp = mapping["sport_param"]
        lg = mapping["league"]
        sb = mapping.get("sportsbook")
        ck = f"probe_{sp}"
        if lg:
            ck += f"_{lg.replace(' ', '_')}"
        if sb:
            ck += f"_{sb}"
        if event_date:
            ck += f"_{event_date}"
        result: dict[str, Any] = {
            "sport_param": sp,
            "league": lg,
            "sportsbook": sb,
            "status_code": None,
            "games_count": 0,
            "error": None,
            "first_matchup": None,
        }
        try:
            odds = _sharpen_request(sp, lg, event_date, api_key, base_url, markets, ck, sportsbook=sb)
            result["games_count"] = len(odds)
            result["status_code"] = 200
            if odds:
                first = odds[0]
                result["first_matchup"] = f"{first.get('away_team')} @ {first.get('home_team')} [{first.get('commence_time', '')}]"
        except SharpRateLimitError as exc:
            result["status_code"] = 429
            result["error"] = "Rate limited (429)"
        except SharpAPIError as exc:
            result["status_code"] = getattr(exc, "status_code", None)
            result["error"] = str(exc)[:200]
        except Exception as exc:
            result["error"] = str(exc)[:200]
        results.append(result)
        # If this mapping worked, cache it as the primary key
        if result["games_count"] > 0 and not _cache_read("baseball_mlb"):
            _cache_write("baseball_mlb", odds)
    return results


def get_odds(
    sport: str = "mlb",
    league: str | None = None,
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch odds for any supported sport from the Sharp API.

    For MLB, tries sport_param/baseball + league MLB with default sportsbook
    first, then secondary sportsbook, then no sportsbook, then fallback
    sport/league mappings.

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

    # ── MLB: try default sportsbook → secondary → none → probe ───────────
    if sport_lower == "mlb":
        default_sb = default_sportsbook()
        secondary_sb = secondary_sportsbook()
        sportsbooks_to_try = [default_sb, secondary_sb, None]
        for sb in sportsbooks_to_try:
            ck = cfg["cache_key"]
            if league:
                ck = f"{ck}_{league}"
            if sb:
                ck = f"{ck}_{sb}"
            if event_date:
                ck = f"{ck}_{event_date}"
            cached = _cache_read(ck)
            if cached is not None:
                return cached
            try:
                odds = _sharpen_request(
                    cfg["sport_param"], league or cfg["league"], event_date,
                    api_key, base_url, cfg["markets"], ck,
                    sportsbook=sb,
                )
                if odds:
                    logger.info(
                        "Sharp API fetched %d MLB odds events (sportsbook=%s)",
                        len(odds), sb or "none",
                    )
                    return odds
            except (SharpAPIError, SharpRateLimitError):
                logger.warning("Sharp MLB mapping failed (sportsbook=%s)", sb)
        # All sportsbook attempts returned 0 — probe full mapping table
        logger.info("Sharp 0 events for all MLB sportsbook combos — probing full fallbacks...")
        probe_results = probe_sharp_mlb(event_date=event_date)
        for pr in probe_results:
            if pr["games_count"] > 0:
                sb_param = pr.get("sportsbook") or default_sb
                ck_final = cfg["cache_key"]
                if pr["league"]:
                    ck_final = f"{ck_final}_{pr['league']}"
                if sb_param:
                    ck_final = f"{ck_final}_{sb_param}"
                if event_date:
                    ck_final = f"{ck_final}_{event_date}"
                return _sharpen_request(
                    pr["sport_param"], pr["league"], event_date,
                    api_key, base_url, cfg["markets"], ck_final,
                    sportsbook=sb_param,
                )
        return []

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
    }
