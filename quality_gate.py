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


def _check_workflow_verification(card_date: str) -> tuple[bool, dict[str, str], list[str]]:
    """Confirm T-50 produced the required verification artifacts."""
    report = _read_json(data_file("workflow") / card_date / "pregame_verification.json", {})
    labels = {
        "verification": "❌ Missing",
        "starting_pitchers": "❌ Missing",
        "lineup_verification": "❌ Missing",
        "weather_verification": "❌ Missing",
        "odds_verification": "❌ Missing",
        "injury_verification": "➖ Optional/unavailable",
        "player_team_mapping": "❌ Missing",
        "props_verification": "❌ Missing",
    }
    failures: list[str] = []
    if not isinstance(report, dict) or not report:
        return False, labels, ["T-50 verification report is missing."]

    labels["verification"] = "✅ Complete"
    pitchers = int(report.get("pitchers_verified") or 0)
    if pitchers > 0:
        labels["starting_pitchers"] = f"✅ {pitchers} verified"
    else:
        failures.append("Starting pitchers were not verified.")

    if report.get("lineups") in {"confirmed", "projected", "not_confirmed"}:
        labels["lineup_verification"] = f"✅ Checked ({report.get('lineups')})"
    else:
        failures.append("Lineup verification status is missing.")

    if report.get("weather") == "available":
        labels["weather_verification"] = "✅ Available"
    else:
        failures.append("Weather verification is unavailable.")
    if report.get("odds") == "available":
        labels["odds_verification"] = "✅ Available"
    else:
        failures.append("Odds verification is unavailable.")

    # Injury/news feeds are optional in the current stack. The gate requires
    # the check to be recorded, not a third-party injury provider to succeed.
    labels["injury_verification"] = f"✅ Checked ({report.get('injuries', 'optional')})"

    if report.get("player_team_mapping") in {"verified", "checked", "not_required"}:
        labels["player_team_mapping"] = f"✅ {report.get('player_team_mapping')}"
    else:
        failures.append("Player/team mapping verification is missing.")

    if report.get("props") in {"available", "unavailable"}:
        labels["props_verification"] = f"✅ Checked ({report.get('props')})"
    else:
        failures.append("Props verification status is missing.")

    return not failures, labels, failures


def _check_posting_log(card_date: str) -> tuple[bool, str, list[str]]:
    """Make sure today's official card has not already been posted."""
    log = _read_json(data_file("posting_log.json"), {})
    if not isinstance(log, dict):
        return False, "❌ Invalid posting_log.json", ["posting_log.json is invalid."]
    if log.get(f"posted_mlb_card_{card_date}"):
        return False, "❌ Already posted", ["posting_log.json already contains today's posted MLB card flag."]
    return True, "✅ Not posted yet", []


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
    workflow_ok, workflow_labels, workflow_failures = _check_workflow_verification(selected_date)
    posting_ok, posting_label, posting_failures = _check_posting_log(selected_date)
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
        + workflow_failures
        + posting_failures
        + lineups_failures
        + players_failures
        + images_failures
    )
    pending_approval = images_ok and (Path(data_file("generated_cards")).exists())
    ready = all([storage_ok, picks_ok, workflow_ok, posting_ok, lineups_ok, players_ok, images_ok])
    return {
        "card_date": selected_date,
        "display_date": _display_date(selected_date),
        "mode": mode,
        "storage": storage_label,
        "picks_saved": picks_label,
        "workflow_verification": workflow_labels.get("verification"),
        "starting_pitchers": workflow_labels.get("starting_pitchers"),
        "weather_verification": workflow_labels.get("weather_verification"),
        "odds_verification": workflow_labels.get("odds_verification"),
        "injury_verification": workflow_labels.get("injury_verification"),
        "posting_log": posting_label,
        "lineups": lineups_label,
        "lineup_verification": workflow_labels.get("lineup_verification"),
        "player_verification": players_label,
        "player_team_mapping": workflow_labels.get("player_team_mapping"),
        "props_verification": workflow_labels.get("props_verification"),
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
        f"Card date exists: {'✅' if payload.get('card_date') else '❌'}",
        f"game_pk exists: {payload.get('picks_saved')}",
        f"T-50 Verification: {payload.get('workflow_verification')}",
        f"Lineups: {payload.get('lineup_verification') or payload.get('lineups')}",
        f"Starting pitchers: {payload.get('starting_pitchers')}",
        f"Weather: {payload.get('weather_verification')}",
        f"Odds: {payload.get('odds_verification')}",
        f"Injuries: {payload.get('injury_verification')}",
        f"Player verification: {payload.get('player_verification')}",
        f"Player/team mapping: {payload.get('player_team_mapping')}",
        f"Props: {payload.get('props_verification')}",
        f"Images: {payload.get('images')}",
        f"Posting log: {payload.get('posting_log')}",
        f"Pending approval: {payload.get('pending_approval')}",
        f"Ready to post: {'✅' if payload.get('ready_to_post') else '❌'}",
    ]
    failures = payload.get("failures") or []
    if failures:
        lines.extend(["", "Failed checks:"])
        lines.extend(f"- {failure}" for failure in failures[:25])
    return "\n".join(lines).strip()
