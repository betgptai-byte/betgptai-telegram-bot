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
    "SHARP_SPORT_MAP",
    "SOCCER_LEAGUES",
    "build_game_market_context",
    "clear_cache",
    "fetch_mlb_odds",
    "get_odds",
    "get_soccer_odds",
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


def get_odds(
    sport: str = "mlb",
    league: str | None = None,
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch odds for any supported sport from the Sharp API.

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
    cache_key = cfg["cache_key"]
    if league:
        cache_key = f"{cache_key}_{league}"
    if event_date:
        cache_key = f"{cache_key}_{event_date}"

    cached = _cache_read(cache_key)
    if cached is not None:
        logger.debug("Sharp cache HIT for %s", cache_key)
        return cached

    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")

    base_url = _base_url()
    url = f"{base_url}/odds"

    _throttle()

    params: dict[str, str] = {
        "apiKey": api_key,
        "sport": cfg["sport_param"],
        "regions": "us",
        "markets": cfg["markets"],
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    if cfg["league"]:
        params["league"] = cfg["league"]
    if league:
        params["league"] = league
    if event_date:
        params["commenceTimeFrom"] = f"{event_date}T00:00:00Z"
        params["commenceTimeTo"] = f"{event_date}T23:59:59Z"

    try:
        resp = requests.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        raise SharpAPIError(f"Sharp API request failed: {exc}") from exc

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
    logger.info("Sharp API fetched %d odds events for %s", len(normalized), sport_lower)
    return normalized


def get_soccer_odds(league: str | None = None) -> list[dict[str, Any]]:
    """Fetch soccer odds, optionally scoped to a league.

    Args:
        league: Optional league name, e.g. ``"EPL"``, ``"La Liga"``,
                ``"MLS"``.  If ``None``, fetches all soccer odds.

    Returns normalized events in The Odds API shape.
    """
    if league:
        league_clean = league.strip()
        return get_odds(sport="soccer", league=league_clean)
    return get_odds(sport="soccer", league=None)


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
