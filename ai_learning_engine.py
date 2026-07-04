"""BETGPTAI AI Learning Engine Phase 6.

Learning-review mode only:
- Reviews wins/losses after grading.
- Classifies losing picks.
- Saves small suggested weight changes.
- Never applies suggestions until the owner approves them.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from loss_reason_classifier import classify_loss, suggested_weight_changes_from_tags
from mlb_data import get_mlb_schedule
from model_report import load_model_report
from model_weights import (
    LEARNING_REPORTS_DIR,
    MODEL_WEIGHTS_FILE,
    PENDING_WEIGHT_UPDATES_FILE,
    approve_pending_weight_updates,
    clear_pending_weight_updates,
    ensure_model_weights,
    learning_reports_count,
    load_pending_weight_updates,
    save_pending_weight_updates,
)
from results_tracker import load_picks
from storage import data_file


EASTERN = ZoneInfo("America/New_York")
FINAL_RESULTS = {"win", "loss", "push"}


def _display_date(card_date: str) -> str:
    return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _today_picks(card_date: str) -> list[dict[str, Any]]:
    return [
        pick for pick in load_picks()
        if isinstance(pick, dict)
        and pick.get("category") != "parlay_leg"
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
    ]


def _game_index(card_date: str) -> dict[str, dict[str, Any]]:
    try:
        schedule = get_mlb_schedule(card_date)
    except Exception:
        schedule = []
    return {
        str(game.get("game_id") or game.get("game_pk")): game
        for game in schedule
        if game.get("game_id") or game.get("game_pk")
    }


def _props_index(card_date: str) -> dict[str, dict[str, Any]]:
    payload = _read_json(data_file("props_lab.json"), {})
    props: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        day = payload.get(card_date)
        if isinstance(day, dict) and isinstance(day.get("all_props"), list):
            props = [prop for prop in day["all_props"] if isinstance(prop, dict)]
    elif isinstance(payload, list):
        props = [
            prop for prop in payload
            if isinstance(prop, dict) and str(prop.get("card_date") or "") == card_date
        ]
    index = {}
    for prop in props:
        key_parts = [
            str(prop.get("game_pk") or ""),
            str(prop.get("player_name") or "").lower(),
            str(prop.get("prop_type") or prop.get("market_type") or "").lower(),
        ]
        index["|".join(key_parts)] = prop
    return index


def _matching_prop_context(pick: dict[str, Any], props: dict[str, dict[str, Any]]) -> dict[str, Any]:
    text = str(pick.get("pick_text") or pick.get("selection") or "").lower()
    game_pk = str(pick.get("game_pk") or pick.get("game_id") or "")
    for key, prop in props.items():
        if not key.startswith(game_pk + "|"):
            continue
        player = str(prop.get("player_name") or "").lower()
        if player and player in text:
            return prop
    return {}


def _model_factors_used(pick: dict[str, Any], tags: list[str]) -> list[str]:
    market = str(pick.get("market_type") or pick.get("pick_type") or "").lower()
    factors = ["market_value"]
    if market in {"moneyline", "runline", "f5_moneyline"}:
        factors.extend(["starting_pitcher_edge", "bullpen_edge", "team_offense_vs_handedness"])
    if market in {"total", "team_total"}:
        factors.extend(["weather_edge", "park_factor", "statcast_contact", "bullpen_fatigue"])
    if any(tag.startswith("player_") for tag in tags):
        factors.extend(["player_streaks", "player_lineup_spot", "player_team_verification"])
    if "bad_pitch_type_matchup" in tags:
        factors.append("pitch_type_matchup")
    if "bad_bvp_read" in tags:
        factors.append("bvp")
    return sorted(set(factors))


def _merge_suggestions(items: list[dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for suggestions in items:
        for factor, item in suggestions.items():
            target = merged.setdefault(
                factor,
                {"suggested_change": 0.0, "reason_tags": [], "reason": ""},
            )
            target["suggested_change"] += float(item.get("suggested_change", 0))
            target["reason_tags"].extend(item.get("reason_tags", []))
    for factor, item in merged.items():
        change = max(-0.05, min(0.05, item["suggested_change"]))
        tags = sorted(set(item["reason_tags"]))
        item["suggested_change"] = round(change, 4)
        item["reason_tags"] = tags
        item["reason"] = f"Phase 6 review tags: {', '.join(tags)}"
    return merged


def run_learning_review(card_date: str) -> dict[str, Any]:
    """Review graded picks for one card date and save Phase 6 suggestions."""
    ensure_model_weights()
    picks = _today_picks(card_date)
    graded = [pick for pick in picks if pick.get("result") in FINAL_RESULTS]
    losses = [pick for pick in graded if pick.get("result") == "loss"]
    wins = [pick for pick in graded if pick.get("result") == "win"]
    games = _game_index(card_date)
    props = _props_index(card_date)
    model_report = load_model_report(card_date) or {}

    reviewed_losses: list[dict[str, Any]] = []
    suggestion_parts: list[dict[str, dict[str, Any]]] = []
    tag_counter: Counter[str] = Counter()
    missing_data: list[str] = []

    for pick in losses:
        game_pk = str(pick.get("game_pk") or pick.get("game_id") or "")
        game_context = games.get(game_pk, {})
        props_context = _matching_prop_context(pick, props)
        if not game_context:
            missing_data.append(f"{pick.get('pick_id')}: MLB game context missing")
        classification = classify_loss(
            pick,
            game_context=game_context,
            model_report=model_report,
            props_context=props_context,
        )
        tags = classification["loss_reason_tags"]
        tag_counter.update(tags)
        suggestions = suggested_weight_changes_from_tags(tags)
        suggestion_parts.append(suggestions)
        reviewed_losses.append(
            {
                "pick_id": pick.get("pick_id"),
                "card_date": pick.get("card_date") or pick.get("date"),
                "market_type": pick.get("market_type") or pick.get("pick_type"),
                "pick_text": pick.get("pick_text") or pick.get("selection"),
                "game_pk": pick.get("game_pk") or pick.get("game_id"),
                "selected_team_player": pick.get("selected_team") or pick.get("player_name"),
                "result": pick.get("result"),
                "loss_reason_tags": tags,
                "confidence_before": pick.get("confidence_grade") or pick.get("risk_grade"),
                "model_factors_used": _model_factors_used(pick, tags),
                "notes": classification["notes"],
                "suggested_weight_changes": suggestions,
            }
        )

    merged_suggestions = _merge_suggestions(suggestion_parts)
    unknown_variance = tag_counter.get("unknown_variance", 0)
    actionable_losses = max(0, len(losses) - unknown_variance)
    report = {
        "engine": "BETGPTAI AI Learning Engine Phase 6",
        "card_date": card_date,
        "display_date": _display_date(card_date),
        "created_at": datetime.now(EASTERN).isoformat(timespec="seconds"),
        "auto_apply": False,
        "losses_reviewed": len(losses),
        "wins_reviewed": len(wins),
        "unknown_variance": unknown_variance,
        "actionable_losses": actionable_losses,
        "top_loss_reasons": tag_counter.most_common(10),
        "reviewed_losses": reviewed_losses,
        "suggested_adjustments": merged_suggestions,
        "missing_data": missing_data,
    }
    LEARNING_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = LEARNING_REPORTS_DIR / f"{card_date}.json"
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    if merged_suggestions:
        save_pending_weight_updates(card_date, merged_suggestions)
    return report


def load_learning_report(card_date: str) -> dict[str, Any]:
    """Load a saved learning report or return an empty shell."""
    path = LEARNING_REPORTS_DIR / f"{card_date}.json"
    payload = _read_json(path, {})
    return payload if isinstance(payload, dict) else {}


def render_learning_report(report: dict[str, Any]) -> str:
    """Render the owner-only Phase 6 report."""
    reasons = report.get("top_loss_reasons") or []
    suggestions = report.get("suggested_adjustments") or {}
    lines = [
        "🧠 BETGPTAI AI LEARNING REPORT",
        f"📅 Date: {report.get('display_date')}",
        "",
        f"Losses Reviewed: {report.get('losses_reviewed', 0)}",
        f"Wins Reviewed: {report.get('wins_reviewed', 0)}",
        f"Unknown Variance: {report.get('unknown_variance', 0)}",
        f"Actionable Losses: {report.get('actionable_losses', 0)}",
        "",
        "Top Loss Reasons:",
    ]
    if reasons:
        for index, item in enumerate(reasons[:5], start=1):
            tag, count = item
            lines.append(f"{index}. {tag} ({count})")
    else:
        lines.append("1. None yet")
    lines.extend(["", "Suggested Adjustments:"])
    if suggestions:
        for factor, item in suggestions.items():
            change = float(item.get("suggested_change", 0))
            direction = "Increase" if change > 0 else "Reduce"
            lines.append(
                f"- {direction} {factor.replace('_', ' ')} by {abs(change):.2f}: "
                f"{item.get('reason')}"
            )
    else:
        lines.append("- No weight suggestions pending.")
    if report.get("missing_data"):
        lines.extend(["", "Missing Data Logged:"])
        lines.extend(f"- {item}" for item in report["missing_data"][:10])
    lines.extend(["", "Auto Apply: OFF", f"Saved: {report.get('report_path', 'Unavailable')}"])
    return "\n".join(lines).strip()


def render_loss_review(report: dict[str, Any]) -> str:
    """Render every reviewed losing pick."""
    losses = report.get("reviewed_losses") or []
    lines = [
        "🔍 BETGPTAI LOSS REVIEW",
        f"📅 Date: {report.get('display_date')}",
        "",
    ]
    if not losses:
        lines.append("No losing picks reviewed yet.")
        return "\n".join(lines).strip()
    for item in losses[:30]:
        lines.extend(
            [
                f"Pick: {item.get('pick_text')}",
                f"Market: {item.get('market_type')}",
                f"Result: {item.get('result')}",
                f"Tags: {', '.join(item.get('loss_reason_tags') or [])}",
                f"Notes: {' '.join(item.get('notes') or [])}",
                "━━━━━━━━━━━━",
            ]
        )
    return "\n".join(lines).strip()


def render_weight_suggestions() -> str:
    """Render pending owner approval suggestions."""
    pending = load_pending_weight_updates()
    suggestions = pending.get("suggestions") if isinstance(pending.get("suggestions"), dict) else {}
    lines = [
        "⚖️ BETGPTAI WEIGHT SUGGESTIONS",
        f"📅 Date: {pending.get('card_date', 'Unavailable')}",
        "Auto Apply: OFF",
        "",
    ]
    if not suggestions:
        lines.append("No pending weight suggestions.")
        return "\n".join(lines).strip()
    for factor, item in suggestions.items():
        lines.append(
            f"- {factor}: {float(item.get('suggested_change', 0)):+.2f} "
            f"({item.get('reason')})"
        )
    lines.extend(["", "Approve with /approve_weight_update", "Reject with /reject_weight_update"])
    return "\n".join(lines).strip()


def learning_status_payload() -> dict[str, Any]:
    """Return owner-facing learning engine status."""
    ensure_model_weights()
    pending = load_pending_weight_updates()
    last_reports = sorted(LEARNING_REPORTS_DIR.glob("*.json")) if LEARNING_REPORTS_DIR.exists() else []
    last_review = last_reports[-1].stem if last_reports else "None"
    return {
        "available": True,
        "last_review_date": last_review,
        "pending_updates": bool(pending.get("suggestions")),
        "current_weights_file": str(MODEL_WEIGHTS_FILE),
        "reports_saved": learning_reports_count(),
        "auto_apply": False,
    }


def render_learning_status(payload: dict[str, Any]) -> str:
    """Render /learning_status."""
    return (
        "🧠 BETGPTAI AI LEARNING STATUS\n\n"
        f"AI Learning Engine: {'✅ Available' if payload.get('available') else '❌ Unavailable'}\n"
        f"Last Review Date: {payload.get('last_review_date')}\n"
        f"Pending Updates: {'✅ Yes' if payload.get('pending_updates') else '❌ No'}\n"
        f"Current Weights File: {payload.get('current_weights_file')}\n"
        f"Reports Saved: {payload.get('reports_saved')}\n"
        "Auto Apply: OFF"
    )


def approve_weight_update() -> dict[str, Any]:
    """Owner approval entrypoint."""
    return approve_pending_weight_updates()


def reject_weight_update() -> dict[str, Any]:
    """Owner rejection entrypoint."""
    return clear_pending_weight_updates("rejected_by_owner")
