"""MLB player/team verification for BETGPTAI admin props and image cards.

The goal is simple and conservative: before a player prop becomes an admin card
or Anime Vault prompt, confirm the player's current MLB team using MLB Stats API.
If a player is found on a different team than expected, the prop is removed.
"""

from __future__ import annotations

import functools
import re
from typing import Any

import requests


MLB_TEAMS_URL = "https://statsapi.mlb.com/api/v1/teams"
MLB_PEOPLE_SEARCH_URL = "https://statsapi.mlb.com/api/v1/people/search"
MLB_PEOPLE_URL = "https://statsapi.mlb.com/api/v1/people"
REQUEST_TIMEOUT = 15


def _normalize_name(value: Any) -> str:
    """Normalize names for forgiving comparisons."""
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_team(value: Any) -> str:
    """Normalize MLB team names and common short forms."""
    text = _normalize_name(value)
    aliases = {
        "dodgers": "los angeles dodgers",
        "rockies": "colorado rockies",
        "phillies": "philadelphia phillies",
        "marlins": "miami marlins",
        "white sox": "chicago white sox",
        "red sox": "boston red sox",
        "yankees": "new york yankees",
        "mets": "new york mets",
        "cubs": "chicago cubs",
        "brewers": "milwaukee brewers",
        "padres": "san diego padres",
        "giants": "san francisco giants",
        "angels": "los angeles angels",
        "athletics": "athletics",
        "a s": "athletics",
        "d backs": "arizona diamondbacks",
        "diamondbacks": "arizona diamondbacks",
    }
    return aliases.get(text, text)


@functools.lru_cache(maxsize=1)
def _current_roster_index() -> dict[str, dict[str, Any]]:
    """Build a cached player-name index from MLB teams hydrated with rosters."""
    response = requests.get(
        MLB_TEAMS_URL,
        params={"sportId": 1, "hydrate": "roster"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    index: dict[str, dict[str, Any]] = {}
    for team in response.json().get("teams", []):
        team_name = team.get("name") or team.get("teamName")
        for item in (team.get("roster") or {}).get("roster", []):
            person = item.get("person") or {}
            player_name = person.get("fullName")
            if not player_name:
                continue
            index[_normalize_name(player_name)] = {
                "player_id": person.get("id"),
                "player_name": player_name,
                "current_team": team_name,
                "source": "mlb_roster",
            }
    return index


def _search_player_current_team(player_name: str) -> dict[str, Any] | None:
    """Fallback MLB Stats API player search if the hydrated roster misses a name."""
    response = requests.get(
        MLB_PEOPLE_SEARCH_URL,
        params={"names": player_name, "hydrate": "currentTeam"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    people = response.json().get("people", [])
    if not people:
        return None
    exact = _normalize_name(player_name)
    selected = next(
        (person for person in people if _normalize_name(person.get("fullName")) == exact),
        people[0],
    )
    current_team = selected.get("currentTeam") or {}
    return {
        "player_id": selected.get("id"),
        "player_name": selected.get("fullName") or player_name,
        "current_team": current_team.get("name"),
        "source": "mlb_people_search",
    }


def verify_player_team(player_name: str, expected_team: str = "") -> dict[str, Any]:
    """Verify a player's current team against MLB Stats API.

    Args:
        player_name: Player name to verify.
        expected_team: Team the prop/image card expects the player to be on.

    Returns:
        A dictionary with verified, player_id, current_team, expected_team,
        status, and reason.
    """
    clean_name = str(player_name or "").strip()
    clean_expected = str(expected_team or "").strip()
    if not clean_name:
        return {
            "verified": False,
            "player_id": None,
            "current_team": None,
            "expected_team": clean_expected,
            "status": "missing_player_name",
            "reason": "No player name was provided.",
        }

    try:
        indexed = _current_roster_index().get(_normalize_name(clean_name))
        player = indexed or _search_player_current_team(clean_name)
    except Exception as error:
        return {
            "verified": False,
            "player_id": None,
            "current_team": None,
            "expected_team": clean_expected,
            "status": "verification_unavailable",
            "reason": f"MLB Stats API verification failed: {error}",
        }

    if not player or not player.get("current_team"):
        return {
            "verified": False,
            "player_id": player.get("player_id") if player else None,
            "current_team": player.get("current_team") if player else None,
            "expected_team": clean_expected,
            "status": "player_not_found",
            "reason": "Player was not found on a current MLB roster.",
        }

    current_team = str(player.get("current_team") or "")
    if not clean_expected:
        return {
            "verified": True,
            "player_id": player.get("player_id"),
            "current_team": current_team,
            "expected_team": clean_expected,
            "status": "verified_no_expected_team",
            "reason": f"{clean_name} is currently listed with {current_team}.",
        }

    verified = _normalize_team(current_team) == _normalize_team(clean_expected)
    return {
        "verified": verified,
        "player_id": player.get("player_id"),
        "current_team": current_team,
        "expected_team": clean_expected,
        "status": "verified" if verified else "team_mismatch",
        "reason": (
            f"{clean_name} is currently listed with {current_team}."
            if verified
            else f"{clean_name} is listed with {current_team}, not {clean_expected}."
        ),
    }


def verify_player_team_by_id(player_id: Any, expected_team: str = "") -> dict[str, Any]:
    """Verify a player's current team using MLB Stats API player ID.

    This is stricter than name lookup and is the preferred source of truth for
    official image-card publishing.
    """
    if not player_id:
        return {
            "verified": False,
            "player_id": None,
            "current_team": None,
            "expected_team": expected_team,
            "status": "missing_player_id",
            "reason": "No MLB player ID was available for verification.",
        }
    try:
        response = requests.get(
            f"{MLB_PEOPLE_URL}/{player_id}",
            params={"hydrate": "currentTeam"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        people = response.json().get("people", [])
    except Exception as error:
        return {
            "verified": False,
            "player_id": player_id,
            "current_team": None,
            "expected_team": expected_team,
            "status": "verification_unavailable",
            "reason": f"MLB Stats API player-ID verification failed: {error}",
        }
    if not people:
        return {
            "verified": False,
            "player_id": player_id,
            "current_team": None,
            "expected_team": expected_team,
            "status": "player_not_found",
            "reason": "MLB Stats API did not return this player ID.",
        }
    person = people[0]
    current_team = (person.get("currentTeam") or {}).get("name")
    if not current_team:
        return {
            "verified": False,
            "player_id": player_id,
            "player_name": person.get("fullName"),
            "current_team": None,
            "expected_team": expected_team,
            "status": "not_active",
            "reason": "Player does not have a current MLB team in MLB Stats API.",
        }
    verified = not expected_team or _normalize_team(current_team) == _normalize_team(expected_team)
    return {
        "verified": verified,
        "player_id": player_id,
        "player_name": person.get("fullName"),
        "current_team": current_team,
        "expected_team": expected_team,
        "status": "verified_active_roster" if verified else "team_mismatch",
        "reason": (
            f"{person.get('fullName')} is active with {current_team}."
            if verified
            else f"{person.get('fullName')} is active with {current_team}, not {expected_team}."
        ),
    }
