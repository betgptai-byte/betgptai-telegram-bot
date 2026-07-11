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
            if mutate:
                continue
            reason = str(prop.get("invalidated_reason") or "Previously invalidated after lineup verification.")
            if _is_pitcher_prop(prop, slate):
                pitcher_count += 1
                bucket = pitcher_invalidations
            elif _is_hitter_prop(prop, slate):
                hitter_count += 1
                bucket = hitter_invalidations
            else:
                continue
            key = _invalidation_key(prop, reason)
            if key in seen:
                duplicates_removed += 1
                continue
            seen.add(key)
            bucket.append({
                "player": str(prop.get("player_name") or prop.get("prop_id") or "Player"),
                "reason": reason,
            })
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


def _best_hit_player_for_date(card_date: str) -> str:
    """Return the player name of today's public Best Hit Prop (if cached)."""
    try:
        from best_hit_prop_image import BEST_HIT_CACHE_FILE
        cache = _read_json(BEST_HIT_CACHE_FILE, {})
    except Exception:
        return ""
    if not isinstance(cache, dict):
        return ""
    entry = cache.get(card_date)
    if not isinstance(entry, dict):
        return ""
    prop = entry.get("prop") if isinstance(entry.get("prop"), dict) else {}
    return str(prop.get("player_name") or "").strip()


def _public_prop_players(card_date: str) -> set[str]:
    """Players that are in a PUBLIC official prop (Best Hit) for the date."""
    player = _best_hit_player_for_date(card_date)
    return {player.lower()} if player else set()


def _admin_only_prop_players(card_date: str) -> set[str]:
    """Players approved as admin-only/watchlist props for the date."""
    try:
        from player_props_engine import APPROVED_PROPS_FILE
        approved = _read_json(APPROVED_PROPS_FILE, {})
    except Exception:
        return set()
    if not isinstance(approved, dict):
        return set()
    names: set[str] = set()
    for prop in approved.values():
        if not isinstance(prop, dict):
            continue
        if str(prop.get("card_date") or "") != card_date:
            continue
        name = str(prop.get("player_name") or "").strip().lower()
        if name:
            names.add(name)
    return names


def _saved_prop_pick_players(card_date: str) -> set[str]:
    """Players with a saved official prop pick (approved_player_prop) for the date."""
    try:
        from results_tracker import load_picks
        picks = load_picks()
    except Exception:
        return set()
    names: set[str] = set()
    for pick in picks:
        if not isinstance(pick, dict):
            continue
        if str(pick.get("card_date") or pick.get("date") or "") != card_date:
            continue
        if pick.get("category") != "approved_player_prop":
            continue
        name = str(pick.get("player_name") or pick.get("selected_team") or "").strip().lower()
        if name:
            names.add(name)
    return names


def _simple_card_prop_players(card_date: str) -> set[str]:
    """Return player names from prop rows in the saved public simple card."""
    try:
        from services.simple_mlb_card import SIMPLE_CARD_DIR
        path = SIMPLE_CARD_DIR / f"{card_date}.json"
        if not path.exists():
            return set()
        card = _read_json(path, {})
    except Exception:
        return set()
    if not isinstance(card, dict):
        return set()
    names: set[str] = set()
    for pick in card.get("picks", []) if isinstance(card.get("picks"), list) else []:
        if not isinstance(pick, dict):
            continue
        market = str(pick.get("market") or pick.get("market_type") or pick.get("category") or "").lower()
        # Never interpret an ML/F5/RL team as a player.  A simple-card row only
        # participates in prop scratch handling when it actually names a player.
        if not pick.get("player_name") or "prop" not in market:
            continue
        name = str(pick.get("player_name") or "").strip().lower()
        if name:
            names.add(name)
    return names


def assess_scratch_public_impact(
    card_date: str,
    slate: list[dict[str, Any]],
    invalidation_payload: dict[str, Any] | None = None,
    *,
    replacement_generated: bool = False,
) -> dict[str, Any]:
    """Assess whether scratched players affect a PUBLIC official card.

    Sources checked per invalidated player:
      * simple_cards/YYYY-MM-DD.json
      * official picks store (picks.json)
      * public posted card payload (Best Hit Prop cache)

    Behaviour:
      * Admin-only / watchlist players -> owner-only alert, no public correction.
      * Public official prop players -> public correction, removal from saved
        official picks, and a replacement only when one qualifies.
    """
    # Callers that just invalidated the props must pass that result: a new scan
    # correctly skips invalidated rows and would otherwise erase the impact.
    scan = invalidation_payload if isinstance(invalidation_payload, dict) else _scratch_scan(card_date, slate, mutate=False)
    scratched = scan.get("scratched", []) if isinstance(scan.get("scratched"), list) else []

    public_players = _public_prop_players(card_date)
    admin_players = _admin_only_prop_players(card_date)
    simple_players = _simple_card_prop_players(card_date)
    official_players = _saved_prop_pick_players(card_date)

    affected_public: list[dict[str, str]] = []
    admin_only: list[str] = []
    for raw in scratched:
        name = str(raw).strip().lower()
        if not name:
            continue
        # Only the public Best Hit Prop (and any public simple card entry) counts
        # as a PUBLIC official prop. Approved/admin-only props and their saved
        # picks are owner-only and must NOT trigger a public correction.
        source_matches = []
        if name in simple_players:
            source_matches.append("simple_card")
        if name in official_players:
            source_matches.append("official_picks")
        if name in public_players:
            source_matches.append("public_posted_payload")
        # approved_props is the admin/watchlist store.  A saved approved prop is
        # not public by itself; it needs corroboration from a public payload.
        is_public = bool(name in public_players or name in simple_players or (name in official_players and name not in admin_players))
        if is_public:
            affected_public.append({"player": raw, "sources": ", ".join(source_matches)})
        elif name in admin_players:
            admin_only.append(raw)
        else:
            # Unknown scope: treat as owner-only (safe default).
            admin_only.append(raw)

    public_card_affected = bool(affected_public)
    return {
        "date": card_date,
        "scratched_players": scratched,
        "public_card_affected": public_card_affected,
        "affected_public_picks": affected_public,
        "admin_only_players": admin_only,
        "replacement_generated": replacement_generated,
        "public_correction_needed": public_card_affected,
        "ml_f5_rl_unaffected": True,
        "sources_checked": ["simple_card", "official_picks", "public_posted_payload"],
    }


def _find_replacement_best_hit(card_date: str, slate: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return an alternate qualifying Best Hit candidate (different player)."""
    try:
        from best_hit_prop_image import select_best_hit_prop
        from player_props_engine import build_player_props_lab
        payload = build_player_props_lab(slate, card_date)
        prop, _ = select_best_hit_prop(payload, slate)
    except Exception:
        return None
    if not isinstance(prop, dict):
        return None
    scratched = {p.lower() for p in (_public_prop_players(card_date) | _saved_prop_pick_players(card_date))}
    if str(prop.get("player_name") or "").strip().lower() in scratched:
        return None
    return prop


def render_public_scratch_correction(assessment: dict[str, Any]) -> str:
    """Render the public-facing correction (only call when public impact exists)."""
    if not assessment.get("public_correction_needed"):
        return ""
    players = [item.get("player", "Player") for item in assessment.get("affected_public_picks", [])]
    replacement = "A replacement Best Hit Prop has been generated." if assessment.get("replacement_generated") else "No qualifying replacement — no replacement prop posted."
    lines = [
        "⚠️ BETGPTAI PROP UPDATE",
        "",
        "The following player prop has been removed from today's public card:",
    ]
    lines.extend(f"- {p}" for p in players)
    lines.extend([
        "",
        replacement,
        "This is an educational update only. Verify before any action.",
    ])
    return "\n".join(lines).strip()


def remove_scratched_public_picks(card_date: str, players: list[str]) -> int:
    """Remove saved official prop picks for scratched public players from picks.json.

    Returns the number of picks removed.  Never touches ML/F5/RL picks.
    """
    try:
        from results_tracker import PICKS_FILE, _write_json, load_picks
        picks = load_picks()
    except Exception:
        return 0
    lowered = {str(p).strip().lower() for p in players}
    kept: list[dict[str, Any]] = []
    removed = 0
    for pick in picks:
        if not isinstance(pick, dict):
            kept.append(pick)
            continue
        if str(pick.get("card_date") or pick.get("date") or "") != card_date:
            kept.append(pick)
            continue
        if pick.get("category") != "approved_player_prop":
            kept.append(pick)
            continue
        name = str(pick.get("player_name") or pick.get("selected_team") or "").strip().lower()
        if name in lowered:
            removed += 1
            continue
        kept.append(pick)
    if removed:
        _write_json(PICKS_FILE, kept)
    return removed
