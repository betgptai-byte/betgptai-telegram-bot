"""Optional TheSportsDB metadata enrichment for BETGPTAI.

TheSportsDB is never used for betting models, odds, grading, xG, projections,
or player props. It is a quiet metadata source only: schedules, badges, logos,
stadiums, artwork, thumbnails, and similar display context.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import requests


BASE_ROOT = "https://www.thesportsdb.com/api"
REQUEST_TIMEOUT = 15
UNAVAILABLE = "unavailable"


def thesportsdb_enabled() -> bool:
    """Return True only when the optional provider is explicitly enabled."""
    return os.getenv("THESPORTSDB_ENABLED", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def thesportsdb_api_key() -> str:
    """Read the preferred TheSportsDB key, with legacy env fallback."""
    if not thesportsdb_enabled():
        return ""
    return (
        os.getenv("THESPORTSDB_API_KEY", "").strip()
        or os.getenv("THE_SPORTS_DB_API_KEY", "").strip()
    )


def thesportsdb_version() -> str:
    """Return TheSportsDB API version. v1 is the known free/default path."""
    return os.getenv("THESPORTSDB_VERSION", "v1").strip() or "v1"


def thesportsdb_base_url(api_key: str | None = None) -> str:
    """Return the verified TheSportsDB base URL with trailing slash."""
    selected_key = api_key or thesportsdb_api_key()
    version = thesportsdb_version()
    return f"{BASE_ROOT}/{version}/json/{selected_key}/" if selected_key else ""


def _normalize_team(name: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def thesportsdb_request(
    endpoint: str,
    params: dict[str, Any] | None = None,
    *,
    api_key: str | None = None,
) -> Any:
    """Call one TheSportsDB endpoint when enabled, otherwise stay silent."""
    if not thesportsdb_enabled():
        raise RuntimeError("TheSportsDB is disabled.")
    selected_key = api_key or thesportsdb_api_key()
    if not selected_key:
        raise RuntimeError("TheSportsDB is enabled but THESPORTSDB_API_KEY is missing.")
    response = requests.get(
        f"{thesportsdb_base_url(selected_key)}{endpoint}",
        params=params or {},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def check_thesportsdb_connection(api_key: str | None = None) -> bool:
    """Lightweight owner-only connectivity check."""
    try:
        payload = thesportsdb_request("all_leagues.php", api_key=api_key)
        return isinstance(payload, dict) and isinstance(payload.get("leagues"), list)
    except Exception:
        return False


def thesportsdb_status_label() -> str:
    """Return /status wording for this optional provider."""
    if not thesportsdb_enabled():
        return "➖ Disabled"
    if not thesportsdb_api_key():
        return "➖ Not Configured"
    return "✅ Connected" if check_thesportsdb_connection() else "➖ Optional unavailable"


def get_soccer_events(game_date: str, api_key: str | None = None) -> list[dict[str, Any]]:
    """Fetch worldwide soccer events for schedule/artwork enrichment."""
    try:
        payload = thesportsdb_request(
            "eventsday.php",
            {"d": game_date, "s": "Soccer"},
            api_key=api_key,
        )
        events = payload.get("events", []) if isinstance(payload, dict) else []
        return [event for event in events or [] if isinstance(event, dict)]
    except Exception:
        logging.debug("TheSportsDB soccer events unavailable; continuing", exc_info=True)
        return []


def get_baseball_events(game_date: str, api_key: str | None = None) -> list[dict[str, Any]]:
    """Fetch baseball events for backup schedule metadata only."""
    try:
        payload = thesportsdb_request(
            "eventsday.php",
            {"d": game_date, "s": "Baseball"},
            api_key=api_key,
        )
        events = payload.get("events", []) if isinstance(payload, dict) else []
        return [event for event in events or [] if isinstance(event, dict)]
    except Exception:
        logging.debug("TheSportsDB baseball events unavailable; continuing", exc_info=True)
        return []


def get_event_details(event_id: str, api_key: str | None = None) -> dict[str, Any] | str:
    """Return optional event artwork/thumbnails/details for display cards."""
    try:
        payload = thesportsdb_request(
            "lookupevent.php",
            {"id": event_id},
            api_key=api_key,
        )
        events = payload.get("events", []) if isinstance(payload, dict) else []
        event = next((item for item in events or [] if isinstance(item, dict)), None)
        if not event:
            return UNAVAILABLE
        return {
            "event_id": event.get("idEvent"),
            "name": event.get("strEvent"),
            "league": event.get("strLeague"),
            "venue": event.get("strVenue"),
            "thumb": event.get("strThumb"),
            "poster": event.get("strPoster"),
            "banner": event.get("strBanner"),
            "square": event.get("strSquare"),
            "video": event.get("strVideo"),
        }
    except Exception:
        logging.debug("TheSportsDB event details unavailable; continuing", exc_info=True)
        return UNAVAILABLE


def get_league_standings(
    league_id: str,
    season: str,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return optional league standings for context/display only."""
    try:
        payload = thesportsdb_request(
            "lookuptable.php",
            {"l": league_id, "s": season},
            api_key=api_key,
        )
        table = payload.get("table", []) if isinstance(payload, dict) else []
        return [row for row in table or [] if isinstance(row, dict)]
    except Exception:
        logging.debug("TheSportsDB standings unavailable; continuing", exc_info=True)
        return []


def get_tv_schedule(game_date: str, api_key: str | None = None) -> list[dict[str, Any]]:
    """Return optional TV schedule metadata when available."""
    try:
        payload = thesportsdb_request(
            "eventstv.php",
            {"d": game_date},
            api_key=api_key,
        )
        tv_events = payload.get("tvevents", []) if isinstance(payload, dict) else []
        return [event for event in tv_events or [] if isinstance(event, dict)]
    except Exception:
        logging.debug("TheSportsDB TV schedule unavailable; continuing", exc_info=True)
        return []


def get_team_metadata(team_name: str, api_key: str | None = None) -> dict[str, Any] | str:
    """Return display-only team metadata such as badge, stadium, and artwork."""
    try:
        payload = thesportsdb_request(
            "searchteams.php",
            {"t": team_name},
            api_key=api_key,
        )
        teams = payload.get("teams", []) if isinstance(payload, dict) else []
        selected = next(
            (
                team for team in teams or []
                if isinstance(team, dict)
                and _normalize_team(team.get("strTeam")) == _normalize_team(team_name)
            ),
            None,
        )
        if not selected:
            return UNAVAILABLE
        return {
            "team_id": selected.get("idTeam"),
            "name": selected.get("strTeam"),
            "short_name": selected.get("strTeamShort"),
            "league": selected.get("strLeague"),
            "country": selected.get("strCountry"),
            "badge": selected.get("strBadge"),
            "logo": selected.get("strLogo"),
            "jersey": selected.get("strEquipment"),
            "stadium": selected.get("strStadium"),
            "stadium_location": selected.get("strStadiumLocation"),
            "stadium_thumb": selected.get("strStadiumThumb"),
        }
    except Exception:
        logging.debug("TheSportsDB team metadata unavailable; continuing", exc_info=True)
        return UNAVAILABLE


def merge_baseball_metadata(
    slate: list[dict[str, Any]],
    game_date: str,
    api_key: str | None = None,
) -> list[dict[str, Any]]:
    """Add baseball badges/stadium metadata without touching analysis logic."""
    if not thesportsdb_enabled():
        return slate
    selected_key = api_key or thesportsdb_api_key()
    if not selected_key:
        return slate
    for game in slate:
        try:
            game["thesportsdb_metadata"] = {
                "date": game_date,
                "away_team": get_team_metadata(str(game.get("away_team", "")), selected_key),
                "home_team": get_team_metadata(str(game.get("home_team", "")), selected_key),
            }
        except Exception:
            game.setdefault("thesportsdb_metadata", UNAVAILABLE)
    return slate
