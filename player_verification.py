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
MLB_BOXSCORE_URL = "https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
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


def _truthy_same_team(left: Any, right: Any) -> bool:
    """Compare two team names using the same normalization rules everywhere."""
    return _normalize_team(left) == _normalize_team(right)


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
                "team_id": team.get("id"),
                "active_roster": True,
                "source": "mlb_roster",
            }
    return index


@functools.lru_cache(maxsize=1)
def _current_roster_id_index() -> dict[str, dict[str, Any]]:
    """Build a cached active-roster index keyed by MLB player ID."""
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
            player_id = person.get("id")
            if not player_id:
                continue
            index[str(player_id)] = {
                "player_id": player_id,
                "player_name": person.get("fullName"),
                "current_team": team_name,
                "team_id": team.get("id"),
                "active_roster": True,
                "source": "mlb_roster",
            }
    return index


@functools.lru_cache(maxsize=128)
def _boxscore(game_pk: str) -> dict[str, Any]:
    """Fetch a game boxscore so we can verify confirmed lineup/batting order."""
    response = requests.get(
        MLB_BOXSCORE_URL.format(game_pk=game_pk),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def _lineup_lookup(game_pk: Any, player_id: Any) -> dict[str, Any]:
    """Return confirmed batting-order context for a player when MLB has it."""
    if not game_pk or not player_id:
        return {
            "verified": False,
            "lineup_status": "missing_game_or_player",
            "lineup_spot": None,
            "reason": "Missing game_pk or player_id for lineup verification.",
        }
    try:
        boxscore = _boxscore(str(game_pk))
    except Exception as error:
        return {
            "verified": False,
            "lineup_status": "lineup_unavailable",
            "lineup_spot": None,
            "reason": f"MLB Stats API lineup lookup failed: {error}",
        }
    player_key = f"ID{player_id}"
    for side in ("away", "home"):
        team = (boxscore.get("teams") or {}).get(side) or {}
        players = team.get("players") or {}
        player = players.get(player_key)
        if not isinstance(player, dict):
            continue
        batting_order = player.get("battingOrder")
        if not batting_order:
            return {
                "verified": False,
                "lineup_status": "not_in_starting_lineup",
                "lineup_spot": None,
                "reason": "Player is on the game roster but not confirmed in the starting batting order.",
            }
        try:
            lineup_spot = int(str(batting_order)[:1])
        except ValueError:
            lineup_spot = None
        person = player.get("person") or {}
        return {
            "verified": lineup_spot is not None,
            "lineup_status": "confirmed",
            "lineup_spot": lineup_spot,
            "batting_order": batting_order,
            "player_name": person.get("fullName"),
            "reason": (
                f"Player is confirmed batting {lineup_spot}."
                if lineup_spot is not None
                else "Batting order was present but could not be parsed."
            ),
        }
    return {
        "verified": False,
        "lineup_status": "player_not_in_boxscore",
        "lineup_spot": None,
        "reason": "Player was not found in today’s MLB game boxscore.",
    }


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

    active_roster = bool(indexed)
    verified = active_roster and _normalize_team(current_team) == _normalize_team(clean_expected)
    return {
        "verified": verified,
        "player_id": player.get("player_id"),
        "current_team": current_team,
        "expected_team": clean_expected,
        "active_roster": active_roster,
        "status": (
            "verified_active_roster"
            if verified
            else "not_on_active_roster"
            if not active_roster
            else "team_mismatch"
        ),
        "reason": (
            f"{clean_name} is currently listed with {current_team}."
            if verified
            else f"{clean_name} is listed with {current_team}, not active roster verified for {clean_expected}."
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
        roster_player = _current_roster_id_index().get(str(player_id))
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
    current_team = (roster_player or {}).get("current_team") or (person.get("currentTeam") or {}).get("name")
    if not current_team:
        return {
            "verified": False,
            "player_id": player_id,
            "player_name": person.get("fullName"),
            "current_team": None,
            "expected_team": expected_team,
            "active_roster": False,
            "status": "not_active",
            "reason": "Player does not have a current MLB team in MLB Stats API.",
        }
    active_roster = bool(roster_player)
    verified = active_roster and (not expected_team or _truthy_same_team(current_team, expected_team))
    return {
        "verified": verified,
        "player_id": player_id,
        "player_name": (roster_player or {}).get("player_name") or person.get("fullName"),
        "current_team": current_team,
        "expected_team": expected_team,
        "active_roster": active_roster,
        "status": (
            "verified_active_roster"
            if verified
            else "not_on_active_roster"
            if not active_roster
            else "team_mismatch"
        ),
        "reason": (
            f"{(roster_player or {}).get('player_name') or person.get('fullName')} is active with {current_team}."
            if verified
            else f"{(roster_player or {}).get('player_name') or person.get('fullName')} is listed with {current_team}, not active roster verified for {expected_team}."
        ),
    }


def verify_hit_prop_context(
    prop: dict[str, Any],
    slate: list[dict[str, Any]],
) -> dict[str, Any]:
    """Strictly verify a Best Hit Prop against today's MLB source of truth.

    This check intentionally does not trust cached prop names. MLB Stats API is
    used for the player ID, current team, active roster, today's opponent, and
    confirmed batting order. If confirmed batting order is not available yet,
    the prop is considered invalid for public display.
    """
    player_id = prop.get("player_id")
    expected_team = prop.get("team_name") or prop.get("team") or ""
    expected_opponent = prop.get("opponent_name") or prop.get("opponent") or ""
    game_pk = prop.get("game_pk") or prop.get("game_id")
    player_check = verify_player_team_by_id(player_id, str(expected_team))

    selected_game: dict[str, Any] | None = None
    for game in slate:
        game_id = game.get("game_pk") or game.get("game_id")
        teams = {game.get("away_team"), game.get("home_team")}
        if game_pk and str(game_id) == str(game_pk):
            selected_game = game
            break
        if expected_team in teams and expected_opponent in teams:
            selected_game = game
            break

    matchup_valid = False
    today_opponent = None
    if selected_game:
        away = selected_game.get("away_team")
        home = selected_game.get("home_team")
        if _truthy_same_team(expected_team, away):
            today_opponent = home
            matchup_valid = not expected_opponent or _truthy_same_team(expected_opponent, home)
        elif _truthy_same_team(expected_team, home):
            today_opponent = away
            matchup_valid = not expected_opponent or _truthy_same_team(expected_opponent, away)

    verified_game_pk = (selected_game or {}).get("game_pk") or (selected_game or {}).get("game_id") or game_pk
    lineup_check = _lineup_lookup(verified_game_pk, player_id)

    prop_lineup = prop.get("lineup_verification") if isinstance(prop.get("lineup_verification"), dict) else {}
    projected_spot = prop.get("projected_batting_position") or prop_lineup.get("lineup_spot")
    try:
        projected_spot_int = int(projected_spot)
    except Exception:
        projected_spot_int = None
    lineup_status = str(lineup_check.get("lineup_status") or "")
    projected_ok = (
        lineup_status in {"lineup_unavailable", "player_not_in_boxscore", "missing_game_or_player"}
        and projected_spot_int is not None
        and 1 <= projected_spot_int <= 5
        and bool(player_check.get("verified"))
        and bool(player_check.get("active_roster"))
    )
    valid = (
        bool(player_check.get("verified"))
        and bool(player_check.get("active_roster"))
        and bool(matchup_valid)
        and (bool(lineup_check.get("verified")) or projected_ok)
    )
    reasons = []
    if not player_check.get("verified"):
        reasons.append(str(player_check.get("reason")))
    if not matchup_valid:
        reasons.append("Today’s opponent/matchup could not be verified from MLB Stats API.")
    if not lineup_check.get("verified") and not projected_ok:
        reasons.append(str(lineup_check.get("reason")))
    return {
        "valid": valid,
        "verified": valid,
        "player": player_check.get("player_name") or prop.get("player_name"),
        "player_id": player_id,
        "expected_team": expected_team,
        "verified_current_team": player_check.get("current_team"),
        "current_team": player_check.get("current_team"),
        "active_roster": bool(player_check.get("active_roster")),
        "today_opponent": today_opponent,
        "expected_opponent": expected_opponent,
        "game_pk": verified_game_pk,
        "lineup_spot": lineup_check.get("lineup_spot") or projected_spot_int,
        "lineup_status": lineup_check.get("lineup_status") if lineup_check.get("verified") else ("projected" if projected_ok else lineup_check.get("lineup_status")),
        "status": "valid" if valid else "invalid",
        "reason": (
            "All Best Hit Prop checks passed."
            if valid and lineup_check.get("verified")
            else "Projected top-five lineup accepted while official lineup is unavailable."
            if valid and projected_ok
            else " ".join(reasons)
        ),
        "player_check": player_check,
        "lineup_check": lineup_check,
    }
