"""Production-safe ESPN MLB JSON API client.

This client uses ESPN JSON endpoints only. It never scrapes ESPN HTML.

The attached user notes did not include concrete endpoint paths, so this module
uses ESPN's standard public JSON API shapes:
- site.api.espn.com/apis/site/v2/sports/baseball/mlb
- sports.core.api.espn.com/v2/sports/baseball/leagues/mlb

All requests are cached for five minutes, retry twice after the first failure,
timeout after ten seconds, and log failures to DATA_DIR/logs/api.log.
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from typing import Any

import requests

from storage import data_file


SITE_BASE = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb"
CORE_BASE = "https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb"
TIMEOUT_SECONDS = 10
RETRIES = 2
CACHE_SECONDS = 300

_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}


class ESPNClientError(Exception):
    """Raised internally when ESPN JSON data cannot be fetched."""


def _now_ts() -> float:
    return time.time()


def _log_failure(endpoint: str, error: Exception | str, recovery: str = "return_empty_payload") -> None:
    log_path = data_file("logs") / "api.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "component": "espn_client",
        "endpoint": endpoint,
        "error": str(error),
        "recovery": recovery,
    }
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _date_key(value: str | date | datetime | None = None) -> str:
    if value is None:
        return date.today().strftime("%Y%m%d")
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if "-" in text:
        try:
            return datetime.fromisoformat(text).strftime("%Y%m%d")
        except ValueError:
            pass
    return text.replace("/", "").replace("-", "")


def _cache_key(url: str, params: dict[str, Any] | None) -> tuple[str, str]:
    return url, json.dumps(params or {}, sort_keys=True, default=str)


def _get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    key = _cache_key(url, params)
    cached = _CACHE.get(key)
    if cached and _now_ts() - cached[0] < CACHE_SECONDS:
        return cached[1]

    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ESPNClientError("ESPN returned non-object JSON")
            _CACHE[key] = (_now_ts(), payload)
            return payload
        except Exception as error:
            last_error = error
            if attempt < RETRIES:
                time.sleep(0.35 * (attempt + 1))
                continue
    _log_failure(url, last_error or "unknown ESPN error")
    return {}


def get_scoreboard(date: str | date | datetime | None = None) -> dict[str, Any]:
    """Return ESPN MLB scoreboard for one date."""
    return _get_json(f"{SITE_BASE}/scoreboard", {"dates": _date_key(date)})


def get_summary(event_id: str | int) -> dict[str, Any]:
    """Return ESPN event summary/game context."""
    return _get_json(f"{SITE_BASE}/summary", {"event": event_id})


def get_team(team_id: str | int) -> dict[str, Any]:
    """Return ESPN team metadata."""
    return _get_json(f"{SITE_BASE}/teams/{team_id}")


def get_team_roster(team_id: str | int) -> dict[str, Any]:
    """Return ESPN team roster."""
    return _get_json(f"{SITE_BASE}/teams/{team_id}/roster")


def get_team_schedule(team_id: str | int) -> dict[str, Any]:
    """Return ESPN team schedule."""
    return _get_json(f"{SITE_BASE}/teams/{team_id}/schedule")


def get_team_injuries(team_id: str | int) -> dict[str, Any]:
    """Return ESPN team injuries. Injuries are ESPN-primary in BETGPTAI."""
    return _get_json(f"{SITE_BASE}/teams/{team_id}/injuries")


def get_team_transactions(team_id: str | int) -> dict[str, Any]:
    """Return ESPN team transactions."""
    return _get_json(f"{SITE_BASE}/teams/{team_id}/transactions")


def get_standings() -> dict[str, Any]:
    """Return ESPN MLB standings."""
    return _get_json(f"{SITE_BASE}/standings")


def get_athlete(player_id: str | int) -> dict[str, Any]:
    """Return ESPN athlete profile."""
    return _get_json(f"{CORE_BASE}/athletes/{player_id}")


def get_athlete_gamelog(player_id: str | int) -> dict[str, Any]:
    """Return ESPN athlete game log when available."""
    return _get_json(f"{SITE_BASE}/athletes/{player_id}/gamelog")


def get_athlete_splits(player_id: str | int) -> dict[str, Any]:
    """Return ESPN athlete splits when available."""
    return _get_json(f"{SITE_BASE}/athletes/{player_id}/splits")


def get_odds(event_id: str | int) -> dict[str, Any]:
    """Return ESPN odds/pickcenter data for an event when available."""
    summary = get_summary(event_id)
    return {
        "event_id": event_id,
        "odds": summary.get("odds") or summary.get("pickcenter") or {},
    }


def get_probabilities(event_id: str | int) -> dict[str, Any]:
    """Return ESPN win probability feed when available."""
    return _get_json(f"{CORE_BASE}/events/{event_id}/competitions/{event_id}/probabilities")


def get_predictor(event_id: str | int) -> dict[str, Any]:
    """Return ESPN predictor feed when available."""
    return _get_json(f"{CORE_BASE}/events/{event_id}/competitions/{event_id}/predictor")


def get_news(team_id: str | int | None = None) -> dict[str, Any]:
    """Return ESPN MLB news, optionally team-filtered."""
    params = {"team": team_id} if team_id else None
    return _get_json(f"{SITE_BASE}/news", params)


__all__ = [
    "get_scoreboard",
    "get_summary",
    "get_team",
    "get_team_roster",
    "get_team_schedule",
    "get_team_injuries",
    "get_team_transactions",
    "get_standings",
    "get_athlete",
    "get_athlete_gamelog",
    "get_athlete_splits",
    "get_odds",
    "get_probabilities",
    "get_predictor",
    "get_news",
]

