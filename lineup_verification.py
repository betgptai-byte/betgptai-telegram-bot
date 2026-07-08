"""BETGPTAI lineup verification workflow.

Lineups are important for player props, but they should not block core MLB
markets. This module uses a simple three-state model:

- Projected: usable for admin/player-prop research when active and top 1-5.
- Confirmed: official MLB batting order is posted.
- Scratched: player is active/on roster context but not in today's starting
  batting order after a lineup is available.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from card_time import official_sports_date
from game_time import parse_game_time
from player_verification import verify_player_team_by_id
from storage import data_file
from time_utils import format_et, now_et, to_et


PROJECTED = "Projected"
CONFIRMED = "Confirmed"
SCRATCHED = "Scratched"
WAITING = "Waiting"
REQUEST_TIMEOUT = 15
HITTER_PROP_TYPES = {
    "hits", "2_plus_hits", "home_runs", "hr_watch", "rbis", "runs",
    "total_bases", "stolen_bases", "walks",
}
PITCHER_PROP_TYPES = {
    "strikeouts", "pitcher_outs_recorded", "earned_runs", "hits_allowed",
}
POSTPONED_STATES = {"postponed", "cancelled", "canceled", "suspended"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)


def _boxscore(game_pk: Any) -> dict[str, Any]:
    from player_verification import _boxscore as cached_boxscore  # local import avoids making it public API

    if not game_pk:
        return {}
    try:
        payload = cached_boxscore(str(game_pk))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _lineup_for_side(game_pk: Any, side: str) -> list[dict[str, Any]]:
    """Return official MLB batting order rows for one side when posted."""
    boxscore = _boxscore(game_pk)
    team = ((boxscore.get("teams") or {}).get(side) or {})
    batting_order = team.get("battingOrder") or []
    players = team.get("players") or {}
    rows: list[dict[str, Any]] = []
    for spot, raw_player_id in enumerate(batting_order, start=1):
        player_id = str(raw_player_id).replace("ID", "")
        player = players.get(f"ID{player_id}") or {}
        person = player.get("person") or {}
        rows.append({
            "player_id": player_id,
            "player_name": person.get("fullName") or player_id,
            "lineup_spot": spot,
            "state": CONFIRMED,
            "source": "MLB Stats API confirmed lineup",
        })
    return rows


def _projected_pool(game: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Use existing enriched hitter pools as the projected lineup source."""
    savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
    fangraphs = game.get("fangraphs") if isinstance(game.get("fangraphs"), dict) else {}
    rows = savant.get(f"{side}_batters") or fangraphs.get(f"{side}_hitter_samples") or []
    output: list[dict[str, Any]] = []
    for spot, row in enumerate([item for item in rows if isinstance(item, dict)][:5], start=1):
        player_name = row.get("player") or row.get("Name") or row.get("name") or "Player TBD"
        if isinstance(player_name, str) and "," in player_name:
            last, first = [part.strip() for part in player_name.split(",", 1)]
            if first and last:
                player_name = f"{first} {last}"
        output.append({
            "player_id": row.get("player_id") or row.get("id") or row.get("batter") or row.get("mlbam_id"),
            "player_name": player_name,
            "lineup_spot": spot,
            "state": PROJECTED,
            "source": "FanGraphs/Savant projected top-five pool",
        })
    return output


def summarize_lineups(slate: list[dict[str, Any]], card_date: str | None = None) -> dict[str, Any]:
    """Summarize today's games into Confirmed/Projected/Waiting states."""
    selected = card_date or official_sports_date(now_et()).isoformat()
    games: list[dict[str, Any]] = []
    confirmed = 0
    projected = 0
    waiting = 0
    first_pitch = None
    for game in slate:
        game_pk = game.get("game_pk") or game.get("game_id")
        game_time = to_et(game.get("game_time"))
        if game_time and (first_pitch is None or game_time < first_pitch):
            first_pitch = game_time
        game_rows: dict[str, Any] = {
            "game_pk": game_pk,
            "away_team": game.get("away_team"),
            "home_team": game.get("home_team"),
            "game_time_et": format_et(game_time),
            "sides": {},
        }
        for side in ("away", "home"):
            official = _lineup_for_side(game_pk, side)
            if official:
                state = CONFIRMED
                confirmed += 1
                rows = official
            else:
                rows = _projected_pool(game, side)
                if rows:
                    state = PROJECTED
                    projected += 1
                else:
                    state = WAITING
                    waiting += 1
            game_rows["sides"][side] = {
                "team": game.get(f"{side}_team") or game.get(f"{side}_team_name") or (game.get("away_team") if side == "away" else game.get("home_team")),
                "state": state,
                "top_five": rows[:5],
            }
        games.append(game_rows)
    next_refresh = None
    if first_pitch:
        refresh_start = first_pitch - timedelta(minutes=90)
        if now_et() >= refresh_start:
            next_refresh = now_et() + timedelta(minutes=5)
        else:
            next_refresh = refresh_start
    payload = {
        "card_date": selected,
        "created_at": now_et().isoformat(timespec="seconds"),
        "games": games,
        "confirmed_lineups": confirmed,
        "projected_lineups": projected,
        "games_waiting": waiting,
        "estimated_next_refresh": format_et(next_refresh),
    }
    _write_json(data_file("lineup_status.json"), payload)
    return payload


def verify_prop_lineup_state(prop: dict[str, Any], slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify one prop candidate against lineup order and active/team mapping."""
    player_id = prop.get("player_id")
    expected_team = str(prop.get("team_name") or prop.get("team") or "")
    player_check = verify_player_team_by_id(player_id, expected_team)
    if not player_check.get("verified") or not player_check.get("active_roster"):
        return {
            "verified": False,
            "state": SCRATCHED,
            "status": "team_or_active_roster_failed",
            "lineup_spot": None,
            "reason": player_check.get("reason") or "Player/team mapping failed.",
            "player_check": player_check,
        }

    game_pk = prop.get("game_pk") or prop.get("game_id")
    selected_game = next(
        (
            game for game in slate
            if str(game.get("game_pk") or game.get("game_id")) == str(game_pk)
        ),
        None,
    )
    side = None
    if selected_game:
        if str(expected_team) == str(selected_game.get("away_team")):
            side = "away"
        elif str(expected_team) == str(selected_game.get("home_team")):
            side = "home"
    official_rows = _lineup_for_side(game_pk, side) if side else []
    if official_rows:
        for row in official_rows:
            if str(row.get("player_id")) == str(player_id):
                return {
                    "verified": True,
                    "state": CONFIRMED,
                    "status": "confirmed",
                    "lineup_spot": row.get("lineup_spot"),
                    "reason": f"Official MLB lineup confirms batting spot {row.get('lineup_spot')}.",
                    "player_check": player_check,
                }
        return {
            "verified": False,
            "state": SCRATCHED,
            "status": "scratched",
            "lineup_spot": None,
            "reason": "Official MLB lineup is posted and player is not in the starting order.",
            "player_check": player_check,
        }

    projected_spot = prop.get("projected_batting_position") or (prop.get("lineup_verification") or {}).get("lineup_spot")
    try:
        projected_spot_int = int(projected_spot)
    except Exception:
        projected_spot_int = None
    if projected_spot_int and 1 <= projected_spot_int <= 5:
        return {
            "verified": True,
            "state": PROJECTED,
            "status": "projected",
            "lineup_spot": projected_spot_int,
            "reason": f"Projected top-five lineup spot {projected_spot_int}; official lineup not posted yet.",
            "player_check": player_check,
        }
    return {
        "verified": False,
        "state": WAITING,
        "status": "waiting",
        "lineup_spot": projected_spot_int,
        "reason": "Official lineup unavailable and projected batting spot is not 1-5.",
        "player_check": player_check,
    }


def _prop_type(prop: dict[str, Any]) -> str:
    return str(prop.get("prop_type") or prop.get("market_type") or "").strip().lower()


def _game_for_prop(prop: dict[str, Any], slate: list[dict[str, Any]]) -> dict[str, Any] | None:
    game_pk = prop.get("game_pk") or prop.get("game_id")
    return next(
        (
            game for game in slate
            if str(game.get("game_pk") or game.get("game_id")) == str(game_pk)
        ),
        None,
    )


def _is_probable_starter(prop: dict[str, Any], game: dict[str, Any] | None) -> bool:
    if not game:
        return False
    player_id = str(prop.get("player_id") or "")
    player_name = str(prop.get("player_name") or "").strip().lower()
    for side in ("away", "home"):
        pitcher_id = str(game.get(f"{side}_pitcher_id") or "")
        pitcher_name = str(game.get(f"{side}_pitcher") or "").strip().lower()
        if player_id and pitcher_id and player_id == pitcher_id:
            return True
        if player_name and pitcher_name and player_name == pitcher_name:
            return True
    return False


def _is_pitcher_prop(prop: dict[str, Any], slate: list[dict[str, Any]]) -> bool:
    prop_type = _prop_type(prop)
    if prop_type in PITCHER_PROP_TYPES:
        return True
    # Protect ambiguous markets, such as a pitcher walk prop, when the player is
    # today's probable starter. Pitchers must never be checked against batting order.
    return _is_probable_starter(prop, _game_for_prop(prop, slate))


def _is_hitter_prop(prop: dict[str, Any], slate: list[dict[str, Any]]) -> bool:
    return _prop_type(prop) in HITTER_PROP_TYPES and not _is_pitcher_prop(prop, slate)


def verify_pitcher_prop_start_state(prop: dict[str, Any], slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify pitcher props by starter status only, never batting lineup."""
    game = _game_for_prop(prop, slate)
    if not game:
        return {
            "verified": False,
            "state": WAITING,
            "status": "game_not_found",
            "reason": "Scheduled game could not be matched.",
        }
    game_status = str(game.get("status") or "").lower()
    if any(state in game_status for state in POSTPONED_STATES):
        return {
            "verified": False,
            "state": SCRATCHED,
            "status": "game_postponed",
            "reason": "Game is postponed/suspended/cancelled.",
        }
    if _is_probable_starter(prop, game):
        return {
            "verified": True,
            "state": CONFIRMED,
            "status": "probable_starter_confirmed",
            "reason": "MLB Stats API still lists pitcher as probable/confirmed starter.",
        }
    starters = " / ".join(str(game.get(f"{side}_pitcher") or "TBD") for side in ("away", "home"))
    return {
        "verified": False,
        "state": SCRATCHED,
        "status": "starting_pitcher_changed",
        "reason": f"Starting pitcher changed or player is no longer listed as probable starter. Current starters: {starters}.",
    }


def _invalidate(prop: dict[str, Any], reason: str) -> None:
    prop["status"] = "invalidated"
    prop["invalidated_reason"] = reason


def _invalidation_key(prop: dict[str, Any], reason: str) -> tuple[str, str, str]:
    return (
        "",
        str(prop.get("player_id") or prop.get("player_name") or ""),
        str(reason or ""),
    )


def _sync_candidate_status(day: dict[str, Any], invalidated: dict[str, str]) -> None:
    candidates = day.get("candidates")
    if not isinstance(candidates, dict):
        return
    for values in candidates.values():
        if not isinstance(values, list):
            continue
        for prop in values:
            if not isinstance(prop, dict):
                continue
            prop_id = str(prop.get("prop_id") or "")
            if prop_id in invalidated:
                _invalidate(prop, invalidated[prop_id])


def _props_for_day(card_date: str) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    path = data_file("props_lab.json")
    payload = _read_json(path, {})
    day = payload.get(card_date) if isinstance(payload, dict) else None
    if not isinstance(day, dict):
        return path, payload if isinstance(payload, dict) else {}, {}, []
    props = [prop for prop in day.get("all_props", []) if isinstance(prop, dict)]
    return path, payload, day, props


def _scratch_scan(card_date: str, slate: list[dict[str, Any]], *, mutate: bool) -> dict[str, Any]:
    path, payload, day, props = _props_for_day(card_date)
    hitter_invalidations: list[dict[str, str]] = []
    pitcher_invalidations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    invalidated_by_id: dict[str, str] = {}
    duplicates_removed = 0
    hitter_count = 0
    pitcher_count = 0
    invalidated_count = 0

    for prop in props:
        if prop.get("status") == "invalidated":
            continue
        if _is_pitcher_prop(prop, slate):
            pitcher_count += 1
            check = verify_pitcher_prop_start_state(prop, slate)
            prop["pitcher_verification"] = check
            if check.get("state") != SCRATCHED:
                continue
            reason = str(check.get("reason") or "Starting pitcher changed/scratched.")
            bucket = pitcher_invalidations
        elif _is_hitter_prop(prop, slate):
            hitter_count += 1
            check = verify_prop_lineup_state(prop, slate)
            prop["lineup_verification"] = check
            if check.get("state") != SCRATCHED:
                continue
            reason = str(check.get("reason") or "Player removed from lineup.")
            bucket = hitter_invalidations
        else:
            continue

        key = _invalidation_key(prop, reason)
        if mutate:
            _invalidate(prop, reason)
            invalidated_count += 1
            if prop.get("prop_id"):
                invalidated_by_id[str(prop.get("prop_id"))] = reason
        if key in seen:
            duplicates_removed += 1
            continue
        seen.add(key)
        item = {
            "player": str(prop.get("player_name") or prop.get("prop_id") or "Player"),
            "reason": reason,
        }
        bucket.append(item)

    changed = invalidated_count if mutate else len(hitter_invalidations) + len(pitcher_invalidations)
    if mutate and changed and day:
        _sync_candidate_status(day, invalidated_by_id)
        _write_json(path, payload)
    return {
        "invalidated": changed,
        "scratched": [item["player"] for item in [*hitter_invalidations, *pitcher_invalidations]],
        "hitter_invalidations": hitter_invalidations,
        "pitcher_invalidations": pitcher_invalidations,
        "props_scanned": len(props),
        "hitter_props_scanned": hitter_count,
        "pitcher_props_scanned": pitcher_count,
        "duplicates_removed": duplicates_removed,
        "best_hit_regenerated": False,
        "best_k_regenerated": False,
        "false_invalidation_protection_active": True,
    }


def invalidate_scratched_props(card_date: str, slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Mark actual scratched props invalid without checking pitchers as hitters."""
    return _scratch_scan(card_date, slate, mutate=True)


def prop_scratch_debug_payload(card_date: str, slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Owner-only diagnostics for scratch invalidation protection."""
    return _scratch_scan(card_date, slate, mutate=False)


def render_prop_scratch_alert(payload: dict[str, Any]) -> str:
    """Render the requested clean split scratch alert."""
    hitter_invalidations = payload.get("hitter_invalidations") if isinstance(payload.get("hitter_invalidations"), list) else []
    pitcher_invalidations = payload.get("pitcher_invalidations") if isinstance(payload.get("pitcher_invalidations"), list) else []
    if not hitter_invalidations and not pitcher_invalidations:
        return ""
    lines = [
        "⚠️ BETGPTAI PROP SCRATCH ALERT",
        "",
        "Hitter Props Invalidated:",
    ]
    lines.extend(
        f"- {item.get('player')} — {item.get('reason')}" for item in hitter_invalidations
    )
    if not hitter_invalidations:
        lines.append("- None")
    lines.extend(["", "Pitcher Props Invalidated:"])
    lines.extend(
        f"- {item.get('player')} — {item.get('reason')}" for item in pitcher_invalidations
    )
    if not pitcher_invalidations:
        lines.append("- None")
    lines.extend([
        "",
        f"Best Hit Prop Regenerated: {'Yes' if payload.get('best_hit_regenerated') else 'No'}",
        f"Best K Prop Regenerated: {'Yes' if payload.get('best_k_regenerated') else 'No'}",
    ])
    return "\n".join(lines).strip()


def render_prop_scratch_debug(payload: dict[str, Any]) -> str:
    """Render owner-only scratch engine diagnostics."""
    hitter_invalidations = payload.get("hitter_invalidations") if isinstance(payload.get("hitter_invalidations"), list) else []
    pitcher_invalidations = payload.get("pitcher_invalidations") if isinstance(payload.get("pitcher_invalidations"), list) else []
    return (
        "🧪 BETGPTAI PROP SCRATCH DEBUG\n\n"
        f"Props scanned: {payload.get('props_scanned', 0)}\n"
        f"Hitter props scanned: {payload.get('hitter_props_scanned', 0)}\n"
        f"Pitcher props scanned: {payload.get('pitcher_props_scanned', 0)}\n"
        f"Hitter invalidations: {len(hitter_invalidations)}\n"
        f"Pitcher invalidations: {len(pitcher_invalidations)}\n"
        f"Duplicates removed: {payload.get('duplicates_removed', 0)}\n"
        f"False invalidation protection active: {'Yes' if payload.get('false_invalidation_protection_active') else 'No'}"
    )


def render_lineup_status(payload: dict[str, Any]) -> str:
    """Owner-facing lineup status summary."""
    return (
        "📋 BETGPTAI LINEUP STATUS\n\n"
        f"Today's Games: {len(payload.get('games') or [])}\n"
        f"Confirmed Lineups: {payload.get('confirmed_lineups', 0)}\n"
        f"Projected Lineups: {payload.get('projected_lineups', 0)}\n"
        f"Games Waiting: {payload.get('games_waiting', 0)}\n"
        f"Estimated Next Refresh: {payload.get('estimated_next_refresh') or 'Unavailable'}"
    )
