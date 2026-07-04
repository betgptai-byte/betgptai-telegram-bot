"""BETGPTAI pre-post quality gate.

This module is intentionally conservative. Public card/image posting should
only continue when tracking, storage, player verification, lineup checks, and
image/prompt assets are all in a healthy state.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from card_time import official_sports_date
from player_verification import verify_player_team_by_id
from storage import data_file, storage_status


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return default


def _mmddyyyy(card_date: str) -> str:
    try:
        return datetime.fromisoformat(card_date).strftime("%m-%d-%Y")
    except ValueError:
        return card_date


def _today_picks(card_date: str) -> list[dict[str, Any]]:
    picks = _read_json(data_file("picks.json"), [])
    if not isinstance(picks, list):
        return []
    return [
        pick for pick in picks
        if isinstance(pick, dict)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
    ]


def _today_props(card_date: str) -> list[dict[str, Any]]:
    payload = _read_json(data_file("props_lab.json"), {})
    if isinstance(payload, dict):
        day = payload.get(card_date)
        if isinstance(day, dict):
            props = day.get("all_props") or []
            return [prop for prop in props if isinstance(prop, dict)]
    if isinstance(payload, list):
        return [
            prop for prop in payload
            if isinstance(prop, dict)
            and str(prop.get("card_date") or "") == card_date
        ]
    return []


def _check_storage() -> tuple[bool, str, list[str]]:
    status = storage_status()
    ok = bool(status.get("results_database_healthy"))
    failures: list[str] = []
    if not ok:
        failures.append("Results storage is not healthy.")
    label = "✅ Healthy" if ok else "❌ Failed"
    return ok, label, failures


def _check_picks(card_date: str) -> tuple[bool, str, list[str]]:
    picks = _today_picks(card_date)
    failures: list[str] = []
    if not picks:
        failures.append("No official picks saved to picks.json for today.")
        return False, "❌ No picks saved", failures
    missing_card_date = [
        pick.get("pick_text") or pick.get("selection") or pick.get("pick_id")
        for pick in picks
        if not pick.get("card_date")
    ]
    missing_game_pk = [
        pick.get("pick_text") or pick.get("selection") or pick.get("pick_id")
        for pick in picks
        if not pick.get("game_pk") and not pick.get("game_id")
    ]
    if missing_card_date:
        failures.append(f"{len(missing_card_date)} pick(s) missing card_date.")
    if missing_game_pk:
        failures.append(f"{len(missing_game_pk)} pick(s) missing game_pk.")
    ok = not missing_card_date and not missing_game_pk
    label = f"✅ {len(picks)} saved" if ok else f"❌ {len(picks)} saved with metadata gaps"
    return ok, label, failures


def _check_player_verification(
    props: list[dict[str, Any]],
    *,
    required: bool = False,
) -> tuple[bool, str, list[str]]:
    """Verify prop player teams when player props exist for today's card."""
    if not props:
        if required:
            return False, "❌ No player prop metadata", ["No player props found for verification."]
        return True, "✅ No player props pending", []
    failures: list[str] = []
    checked = 0
    for prop in props:
        player_id = prop.get("player_id")
        expected_team = str(prop.get("team_name") or prop.get("team") or "")
        if not player_id:
            failures.append(f"{prop.get('player_name', 'Unknown player')} missing player_id.")
            continue
        checked += 1
        result = verify_player_team_by_id(player_id, expected_team)
        if not result.get("verified"):
            failures.append(
                f"{prop.get('player_name', player_id)} team verification failed: "
                f"{result.get('reason')}"
            )
    ok = not failures
    label = f"✅ {checked} verified" if ok else f"❌ {len(failures)} issue(s)"
    return ok, label, failures


def _check_lineups(
    props: list[dict[str, Any]],
    *,
    required: bool = False,
) -> tuple[bool, str, list[str]]:
    if not props:
        if required:
            return False, "❌ No lineup metadata", ["No player props found for lineup checks."]
        return True, "✅ No player props pending", []
    failures: list[str] = []
    checked = 0
    for prop in props:
        lineup = prop.get("lineup_verification")
        if not isinstance(lineup, dict):
            failures.append(f"{prop.get('player_name', 'Unknown player')} missing lineup check.")
            continue
        checked += 1
        if not lineup.get("verified") and not lineup.get("status"):
            failures.append(f"{prop.get('player_name', 'Unknown player')} lineup status not checked.")
    ok = not failures
    label = f"✅ {checked} checked" if ok else f"❌ {len(failures)} issue(s)"
    return ok, label, failures


def _image_paths(card_date: str, mode: str = "general") -> tuple[list[Path], list[Path]]:
    base = data_file("generated_cards")
    iso_dir = base / card_date
    prop_dir = base / _mmddyyyy(card_date)
    if mode == "mlb_images":
        return (
            [iso_dir / f"slide_{index}.png" for index in range(1, 8)],
            [iso_dir / f"slide_{index}_prompt.txt" for index in range(1, 8)],
        )
    if mode == "best_hit":
        return (
            [prop_dir / "best_hit_prop.png"],
            [prop_dir / "best_hit_art_prompt.txt", prop_dir / "best_hit_prop_prompt.txt"],
        )
    return (
        [
            iso_dir / "mlb_auto_card.png",
            iso_dir / "today_pick.png",
            prop_dir / "best_hit_prop.png",
            *[iso_dir / f"slide_{index}.png" for index in range(1, 8)],
        ],
        [
            iso_dir / "mlb_auto_prompt.txt",
            iso_dir / "today_pick_prompt.txt",
            prop_dir / "best_hit_art_prompt.txt",
            prop_dir / "best_hit_prop_prompt.txt",
            *[iso_dir / f"slide_{index}_prompt.txt" for index in range(1, 8)],
        ],
    )


def _check_images(card_date: str, mode: str) -> tuple[bool, str, list[str]]:
    images, prompts = _image_paths(card_date, mode)
    existing_images = [path for path in images if path.exists()]
    existing_prompts = [path for path in prompts if path.exists()]
    failures: list[str] = []
    if mode == "mlb_images":
        missing = [path.name for path in images if not path.exists()]
        if missing:
            failures.append(f"MLB image carousel missing: {', '.join(missing)}")
    elif not existing_images and not existing_prompts:
        failures.append("No generated image or prompt fallback found.")

    ok = not failures
    if existing_images:
        label = f"✅ {len(existing_images)} image(s)"
    elif existing_prompts:
        label = f"✅ Prompt fallback exists ({len(existing_prompts)})"
    else:
        label = "❌ Missing"
    return ok, label, failures


def run_prepost_quality_gate(
    card_date: str | None = None,
    *,
    mode: str = "general",
) -> dict[str, Any]:
    """Run every pre-post check and return a display-ready status payload."""
    selected_date = card_date or official_sports_date().isoformat()
    props = _today_props(selected_date)
    player_checks_required = mode == "best_hit"

    storage_ok, storage_label, storage_failures = _check_storage()
    picks_ok, picks_label, picks_failures = _check_picks(selected_date)
    lineups_ok, lineups_label, lineups_failures = _check_lineups(
        props,
        required=player_checks_required,
    )
    players_ok, players_label, players_failures = _check_player_verification(
        props,
        required=player_checks_required,
    )
    images_ok, images_label, images_failures = _check_images(selected_date, mode)

    failures = (
        storage_failures
        + picks_failures
        + lineups_failures
        + players_failures
        + images_failures
    )
    pending_approval = images_ok and (Path(data_file("generated_cards")).exists())
    ready = all([storage_ok, picks_ok, lineups_ok, players_ok, images_ok])
    return {
        "card_date": selected_date,
        "display_date": _display_date(selected_date),
        "mode": mode,
        "storage": storage_label,
        "picks_saved": picks_label,
        "lineups": lineups_label,
        "player_verification": players_label,
        "images": images_label,
        "pending_approval": "✅ Yes" if pending_approval else "❌ No",
        "ready_to_post": ready,
        "failures": failures,
    }


def render_prepost_quality_gate(payload: dict[str, Any]) -> str:
    """Render the owner-only pre-post check."""
    lines = [
        "🧪 BETGPTAI PRE-POST CHECK",
        f"📅 Date: {payload.get('display_date')}",
        "",
        f"Storage: {payload.get('storage')}",
        f"Picks saved: {payload.get('picks_saved')}",
        f"Lineups: {payload.get('lineups')}",
        f"Player verification: {payload.get('player_verification')}",
        f"Images: {payload.get('images')}",
        f"Pending approval: {payload.get('pending_approval')}",
        f"Ready to post: {'✅' if payload.get('ready_to_post') else '❌'}",
    ]
    failures = payload.get("failures") or []
    if failures:
        lines.extend(["", "Failed checks:"])
        lines.extend(f"- {failure}" for failure in failures[:25])
    return "\n".join(lines).strip()
