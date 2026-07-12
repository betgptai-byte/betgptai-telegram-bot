"""Sharp API — primary odds provider with rate-limiting and caching.

Rate limit: 12 requests/minute (burst-safe with token-bucket).
Cache TTL:  5 minutes per sport key.
Preference: single slate request over per-game requests.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from core.market import MarketContext
from storage import DATA_DIR

logger = logging.getLogger(__name__)

SHARP_CACHE_DIR = DATA_DIR / "sharp_cache"
SHARP_CACHE_TTL = 300  # 5 minutes

# Rate limiter: 12 requests per minute = 1 request per 5 seconds
SHARP_RATE_LIMIT_PER_MINUTE = 12
SHARP_MIN_INTERVAL = 60.0 / SHARP_RATE_LIMIT_PER_MINUTE  # 5.0 seconds


class SharpRateLimitError(Exception):
    """Raised when the Sharp API returns a 429 rate-limit response."""


class SharpAPIError(Exception):
    """Raised on Sharp API HTTP / connectivity failures."""

    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None, response_keys: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.response_keys = response_keys or []


# ── Token-bucket rate limiter ──────────────────────────────────────────────
_last_request_time: float = 0.0


def _throttle() -> None:
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < SHARP_MIN_INTERVAL:
        sleep = SHARP_MIN_INTERVAL - elapsed
        logger.debug("Sharp rate-limiter: sleeping %.2fs", sleep)
        time.sleep(sleep)
    _last_request_time = time.time()


# ── File-based cache ───────────────────────────────────────────────────────
def _cache_path(sport_key: str) -> Path:
    SHARP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = sport_key.replace("/", "_").replace(" ", "_")
    return SHARP_CACHE_DIR / f"{safe}.json"


def _cache_read(sport_key: str) -> list[dict[str, Any]] | None:
    path = _cache_path(sport_key)
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        cached_at = payload.get("cached_at", 0)
        if time.time() - cached_at > SHARP_CACHE_TTL:
            logger.debug("Sharp cache expired for %s", sport_key)
            return None
        return payload.get("data")
    except Exception:
        return None


def _cache_write(sport_key: str, data: list[dict[str, Any]]) -> None:
    try:
        path = _cache_path(sport_key)
        path.write_text(
            json.dumps({"cached_at": time.time(), "data": data}, default=str),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Sharp cache write failed: %s", exc)


def _clear_cache(sport_key: str | None = None) -> None:
    if sport_key:
        _cache_path(sport_key).unlink(missing_ok=True)
    else:
        import shutil
        shutil.rmtree(SHARP_CACHE_DIR, ignore_errors=True)
        SHARP_CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ── Config helpers ─────────────────────────────────────────────────────────
def sharp_api_enabled() -> bool:
    return os.getenv("SHARP_API_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def sharp_api_key() -> str:
    return os.getenv("SHARP_API_KEY", "").strip()


def sharp_api_base_url() -> str:
    return os.getenv("SHARP_API_BASE_URL", "https://api.sharpapi.io/api/v1").rstrip("/")


def odds_api_enabled() -> bool:
    return os.getenv("ODDS_API_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def odds_api_backup_only() -> bool:
    return os.getenv("ODDS_API_BACKUP_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")


# ── Normalization ──────────────────────────────────────────────────────────
def _normalize_team(name: str) -> str:
    import re
    abbreviations = {
        "ari": "Arizona Diamondbacks", "atl": "Atlanta Braves", "bal": "Baltimore Orioles",
        "bos": "Boston Red Sox", "chc": "Chicago Cubs", "cws": "Chicago White Sox",
        "cin": "Cincinnati Reds", "cle": "Cleveland Guardians", "col": "Colorado Rockies",
        "det": "Detroit Tigers", "hou": "Houston Astros", "kc": "Kansas City Royals",
        "laa": "Los Angeles Angels", "lad": "Los Angeles Dodgers", "mia": "Miami Marlins",
        "mil": "Milwaukee Brewers", "min": "Minnesota Twins", "nym": "New York Mets",
        "nyy": "New York Yankees", "ath": "Athletics", "oak": "Athletics",
        "phi": "Philadelphia Phillies", "pit": "Pittsburgh Pirates", "sd": "San Diego Padres",
        "sea": "Seattle Mariners", "sf": "San Francisco Giants", "stl": "St. Louis Cardinals",
        "tb": "Tampa Bay Rays", "tex": "Texas Rangers", "tor": "Toronto Blue Jays",
        "wsh": "Washington Nationals",
    }
    tokens = re.findall(r"[a-z0-9]+", str(name or "").lower())
    tokens = [token for token in tokens if token != "the"]
    if tokens and tokens[0] in abbreviations:
        name = abbreviations[tokens[0]]
    else:
        name = " ".join(tokens)
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    TEAM_ALIASES = {
        "arizonadiamondbacks": "diamondbacks", "arizonadbacks": "diamondbacks",
        "atlantabraves": "braves", "baltimoreorioles": "orioles",
        "bostonredsox": "redsox", "chicagocubs": "cubs",
        "chicagowhitesox": "whitesox", "cincinnatireds": "reds",
        "clevelandguardians": "guardians", "coloradorockies": "rockies",
        "detroittigers": "tigers", "houstonastros": "astros",
        "kansascityroyals": "royals", "losangelesangels": "angels",
        "laangels": "angels", "losangelesdodgers": "dodgers",
        "ladodgers": "dodgers", "miamimarlins": "marlins",
        "milwaukeebrewers": "brewers", "minnesotatwins": "twins",
        "newyorkmets": "mets", "nymets": "mets",
        "newyorkyankees": "yankees", "nyyankees": "yankees",
        "oaklandathletics": "athletics", "sacramentoathletics": "athletics",
        "athletics": "athletics", "philadelphiaphillies": "phillies",
        "pittsburghpirates": "pirates", "sandiegopadres": "padres",
        "sanfranciscogiants": "giants", "seattlemariners": "mariners",
        "stlouiscardinals": "cardinals", "saintlouiscardinals": "cardinals",
        "tampabayrays": "rays", "texasrangers": "rangers",
        "torontobluejays": "bluejays", "washingtonnationals": "nationals",
        "ari": "diamondbacks", "az": "diamondbacks", "atl": "braves",
        "bal": "orioles", "bos": "redsox", "chc": "cubs", "chw": "whitesox",
        "cws": "whitesox", "cin": "reds", "cle": "guardians", "col": "rockies",
        "det": "tigers", "hou": "astros", "kc": "royals", "kcr": "royals",
        "laa": "angels", "lad": "dodgers", "mia": "marlins", "mil": "brewers",
        "min": "twins", "nym": "mets", "nyy": "yankees", "oak": "athletics",
        "ath": "athletics", "phi": "phillies", "pit": "pirates", "sd": "padres",
        "sdp": "padres", "sf": "giants", "sfg": "giants", "sea": "mariners",
        "stl": "cardinals", "tb": "rays", "tbr": "rays", "tex": "rangers",
        "tor": "bluejays", "wsh": "nationals", "was": "nationals",
    }
    return TEAM_ALIASES.get(normalized, normalized)


def _normalize_sharp_event(event: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Sharp API event into the same shape as The Odds API."""
    home = event.get("home_team") or event.get("homeTeam") or ""
    away = event.get("away_team") or event.get("awayTeam") or ""
    commence = event.get("commence_time") or event.get("commenceTime") or event.get("start_time") or ""
    bookmakers_raw = event.get("bookmakers") or event.get("sportsbooks") or []
    if not bookmakers_raw and isinstance(event.get("markets"), list):
        bookmakers_raw = [{
            "key": event.get("sportsbook") or event.get("bookmaker") or "sharpapi",
            "title": event.get("sportsbook") or event.get("bookmaker") or "SharpAPI",
            "markets": event.get("markets"),
        }]
    bookmakers = []
    for book in bookmakers_raw:
        markets_raw = book.get("markets") or []
        markets = []
        for m in markets_raw:
            key = m.get("key") or m.get("market_key") or m.get("market") or ""
            outcomes_raw = m.get("outcomes") or m.get("prices") or m.get("selections") or []
            outcomes = [
                {
                    "name": o.get("name") or o.get("outcome") or o.get("team") or "",
                    "price": o.get("price") or o.get("odds") or o.get("american") or 0,
                    "point": o.get("point") or o.get("spread") or o.get("line"),
                    "description": o.get("description") or "",
                }
                for o in outcomes_raw
            ]
            markets.append({
                "key": key,
                "last_update": m.get("last_update") or m.get("lastUpdate") or "",
                "outcomes": outcomes,
            })
        bk_key = book.get("key") or book.get("bookmaker_key") or ""
        bk_title = book.get("title") or book.get("name") or book.get("bookmaker") or ""
        bk_last = book.get("last_update") or book.get("lastUpdate") or ""
        bookmakers.append({"key": bk_key, "title": bk_title, "last_update": bk_last, "markets": markets})
    return {
        "id": event.get("id") or event.get("event_id") or "",
        "sport_key": event.get("sport_key") or "baseball_mlb",
        "commence_time": commence,
        "home_team": home,
        "away_team": away,
        "bookmakers": bookmakers,
        "provider": "sharpapi",
    }


def parse_sharp_response(data: Any, *, endpoint: str = "/odds") -> dict[str, Any]:
    """Normalize every documented Sharp response envelope without leaking its body."""
    keys = sorted(str(key) for key in data.keys()) if isinstance(data, dict) else []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = next((data[key] for key in ("data", "events", "odds", "results") if isinstance(data.get(key), list)), None)
        if rows is None:
            raise SharpAPIError("parse_failed", code="parse_failed", response_keys=keys)
    else:
        raise SharpAPIError("parse_failed", code="parse_failed", response_keys=keys)
    raw_rows = [row for row in rows if isinstance(row, dict)]
    flat_odds = any(row.get("market_type") or row.get("selection") or row.get("odds_american") is not None for row in raw_rows)
    if flat_odds:
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in raw_rows:
            home = str(row.get("home_team") or row.get("homeTeam") or "")
            away = str(row.get("away_team") or row.get("awayTeam") or "")
            start = str(row.get("start_time") or row.get("event_start_time") or row.get("commence_time") or row.get("commenceTime") or row.get("starts_at") or row.get("scheduled_at") or "")
            # A top-level row `id` is commonly an odds-row id, not a game id.
            # Group flat rows by explicit event/game id plus matchup/time.
            event_id = str(row.get("event_id") or row.get("game_id") or "")
            key = (event_id, _normalize_team(away), _normalize_team(home), start)
            event = grouped.setdefault(key, {"id": event_id, "away_team": away, "home_team": home, "commence_time": start, "bookmakers": []})
            book_key = str(row.get("sportsbook") or row.get("bookmaker") or "sharpapi")
            book = next((item for item in event["bookmakers"] if item["key"] == book_key), None)
            if book is None:
                book = {"key": book_key, "title": book_key, "markets": []}
                event["bookmakers"].append(book)
            raw_market = str(row.get("market_type") or row.get("market") or "").lower()
            market_key = {
                "moneyline": "h2h", "money_line": "h2h", "ml": "h2h", "h2h": "h2h",
                "spread": "spreads", "runline": "spreads", "run_line": "spreads", "game_spread": "spreads",
                "total": "totals", "game_total": "totals", "total_runs": "totals", "over_under": "totals",
                "team_total": "team_totals", "f5_moneyline": "f5_h2h",
                "first_5_moneyline": "f5_h2h", "first_five_moneyline": "f5_h2h", "f5_ml": "f5_h2h",
            }.get(raw_market, raw_market)
            market = next((item for item in book["markets"] if item["key"] == market_key), None)
            if market is None:
                market = {"key": market_key, "outcomes": []}
                book["markets"].append(market)
            selection = row.get("selection") or row.get("outcome") or row.get("team") or ""
            description = row.get("description") or (
                f"{row.get('team')} {selection}" if row.get("team") and str(row.get("team")) not in str(selection) else selection
            )
            market["outcomes"].append({
                "name": selection,
                "price": row.get("odds_american") if row.get("odds_american") is not None else row.get("odds"),
                "point": row.get("line") if row.get("line") is not None else row.get("spread") if row.get("spread") is not None else row.get("handicap"),
                "description": description,
            })
        normalized = [_normalize_sharp_event(event) for event in grouped.values()]
    else:
        normalized = [_normalize_sharp_event(row) for row in raw_rows]
    is_events_endpoint = endpoint.rstrip("/") == "/events"
    return {
        "provider": "sharpapi",
        "events": normalized if is_events_endpoint else normalized,
        "odds": [] if is_events_endpoint else normalized,
        "events_returned": len(normalized),
        "status": "available" if normalized else "unavailable",
        "error": None if normalized else "no_events_returned",
        "top_level_keys": keys if keys else (["<list>"] if isinstance(data, list) else []),
        "odds_rows_returned": len(raw_rows) if flat_odds else len(normalized),
    }


def _best_available_prices(bookmakers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Choose the highest price for each distinct market/outcome/point."""
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for bookmaker in bookmakers:
        for market in bookmaker.get("markets", []):
            for outcome in market.get("outcomes", []):
                price = outcome.get("price")
                if not isinstance(price, (int, float)):
                    continue
                key = (
                    market.get("key"), outcome.get("name"),
                    outcome.get("point"), outcome.get("description"),
                )
                if key not in best or price > best[key]["price"]:
                    best[key] = {
                        "market": market.get("key"),
                        "outcome": outcome.get("name"),
                        "point": outcome.get("point"),
                        "price": price,
                        "description": outcome.get("description"),
                        "bookmaker_key": bookmaker.get("key"),
                        "bookmaker": bookmaker.get("title") or bookmaker.get("name"),
                    }
    return list(best.values())


def _market_context_from_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    context: dict[str, Any] = {"ML": [], "RL": [], "total": [], "team_totals": [], "odds_found": bool(prices)}
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


def build_market_context(
    game: dict[str, Any],
    prices: list[dict[str, Any]],
    provider: str,
    last_updated: str | None = None,
) -> MarketContext:
    ctx = _market_context_from_prices(prices)
    return MarketContext(
        provider=provider,
        moneyline=ctx.get("ML", []),
        runline=ctx.get("RL", []),
        total=ctx.get("total", []),
        team_totals=ctx.get("team_totals", []),
        last_updated=last_updated,
        matched_game_pk=game.get("game_pk") or game.get("game_id"),
        matched_by=provider,
    )


# ── Sharp API fetch ────────────────────────────────────────────────────────
def fetch_mlb_odds() -> list[dict[str, Any]]:
    """Fetch MLB odds from the Sharp API (primary provider).

    Returns normalized events in The Odds API shape so downstream code
    (combine_schedule_and_odds) is provider-agnostic.

    Raises SharpAPIError on connectivity / non-429 failures.
    Raises SharpRateLimitError on 429.
    """
    # The multi-sport client owns the canonical best-odds → sportsbook
    # fallback flow.  Import locally to avoid the module-level compatibility
    # import in sharp_odds_client creating a cycle.
    from api.sharp_odds_client import get_odds
    return get_odds(sport="mlb")

    api_key = sharp_api_key()
    if not api_key:
        raise SharpAPIError("SHARP_API_KEY is missing from .env.")

    base = sharp_api_base_url()
    url = f"{base}/odds"

    cached = _cache_read("baseball_mlb")
    if cached is not None:
        logger.debug("Sharp cache HIT for baseball_mlb")
        return cached

    _throttle()

    params: dict[str, str] = {
        "sport": "baseball",
        "league": "MLB",
        "regions": "us",
        "markets": "h2h,spreads,totals,team_totals",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }

    headers = {"X-API-Key": api_key}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise SharpAPIError(f"Sharp API request failed: {exc}") from exc

    logger.info("Sharp request endpoint=/odds url=%s status=%s", url, resp.status_code)
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

    parsed = parse_sharp_response(data, endpoint="/odds")
    normalized = parsed["odds"]
    logger.info("Sharp response endpoint=/odds keys=%s games=%d events=%d", parsed["top_level_keys"], len(normalized), parsed["events_returned"])
    _cache_write("baseball_mlb", normalized)
    logger.info("Sharp API fetched %d MLB odds events", len(normalized))
    return normalized


def fetch_mlb_odds_cached() -> list[dict[str, Any]]:
    """Return cached odds if fresh, else fetch from Sharp API."""
    cached = _cache_read("baseball_mlb")
    if cached is not None:
        return cached
    return fetch_mlb_odds()


def clear_cache(sport_key: str | None = None) -> None:
    _clear_cache(sport_key)


def health() -> dict[str, Any]:
    """Return Sharp API health status for /status and mission_control."""
    api_key_present = bool(sharp_api_key())
    base = sharp_api_base_url()
    cache_path = _cache_path("baseball_mlb")
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
        "base_url": base,
        "cache_path": str(cache_path),
        "cache_age_seconds": cache_age,
        "cache_fresh": cache_age is not None and cache_age < SHARP_CACHE_TTL,
    }
