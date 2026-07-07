"""BETGPTAI AI Learning Engine Phase 6.

Learning-review + safety-gated apply mode:
- Reviews wins/losses after grading.
- Classifies losing picks.
- Saves small suggested weight changes.
- Applies automatically only when AI_LEARNING_AUTO_APPLY is enabled and every
  safety limit passes; otherwise the owner approval workflow remains active.
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
    ai_learning_auto_apply_enabled,
    approve_pending_weight_updates,
    clear_pending_weight_updates,
    ensure_model_weights,
    latest_weight_history,
    learning_reports_count,
    load_model_weights,
    load_pending_weight_updates,
    maybe_auto_apply_weight_updates,
    save_pending_weight_updates,
    toggle_ai_learning_auto_apply,
    weight_history_files,
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
        loss_reason_confidence = 50 if tags == ["unknown_variance"] else 75
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
                "loss_reason_confidence": loss_reason_confidence,
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
        "loss_reason_confidence": min(
            [float(item.get("loss_reason_confidence", 0)) for item in reviewed_losses],
            default=0.0,
        ) if reviewed_losses else 100.0,
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
    auto_apply_result = maybe_auto_apply_weight_updates(report)
    report["auto_apply"] = bool(auto_apply_result.get("auto_applied"))
    report["auto_apply_result"] = auto_apply_result
    _write_json(report_path, report)
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
    auto_result = report.get("auto_apply_result") if isinstance(report.get("auto_apply_result"), dict) else {}
    auto_label = "ON" if auto_result.get("enabled") else "OFF"
    if auto_result.get("enabled") and not auto_result.get("auto_applied"):
        auto_label = "ON — blocked by safety gates"
    lines.extend(["", f"Auto Apply: {auto_label}", f"Saved: {report.get('report_path', 'Unavailable')}"])
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
        f"Auto Apply: {'ON' if ai_learning_auto_apply_enabled() else 'OFF'}",
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
    latest_history = latest_weight_history()
    return {
        "available": True,
        "last_review_date": last_review,
        "pending_updates": bool(pending.get("suggestions")),
        "current_weights_file": str(MODEL_WEIGHTS_FILE),
        "reports_saved": learning_reports_count(),
        "auto_apply": ai_learning_auto_apply_enabled(),
        "last_applied_date": Path(str(latest_history.get("history_path", ""))).stem if latest_history else "None",
        "last_changes_applied": len(latest_history.get("changes_applied") or []) if latest_history else 0,
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
        f"Auto Apply: {'ON' if payload.get('auto_apply') else 'OFF'}"
    )


def render_learning_auto_status() -> str:
    """Render /learning_auto_status."""
    payload = learning_status_payload()
    return (
        "🧠 BETGPTAI LEARNING AUTO-APPLY\n\n"
        f"AI Learning Auto Apply: {'ON' if payload.get('auto_apply') else 'OFF'}\n"
        f"Last Applied Date: {payload.get('last_applied_date')}\n"
        f"Changes Applied: {payload.get('last_changes_applied')}\n"
        "Current Model Version: BETGPTAI v20.0\n"
        "Safety Limits:\n"
        "- Max single factor/day: 0.05\n"
        "- Max total movement/day: 0.15\n"
        "- Max single factor/7 days: 0.15\n"
        "- Minimum reviewed picks: 5\n"
        "- Minimum loss reason confidence: 70%\n"
        "Next Review: After nightly results grading"
    )


def toggle_learning_auto_apply() -> dict[str, Any]:
    """Owner command entrypoint for toggling auto-apply."""
    return toggle_ai_learning_auto_apply()


def render_weights_admin() -> str:
    """Render current model weights for owner diagnostics."""
    weights = load_model_weights()
    lines = ["⚖️ BETGPTAI MODEL WEIGHTS", "", f"File: {MODEL_WEIGHTS_FILE}", ""]
    for key in sorted(weights):
        lines.append(f"{key}: {float(weights[key]):.4f}")
    return "\n".join(lines).strip()


def render_weight_history_admin() -> str:
    """Render recent model-weight history for owner diagnostics."""
    files = weight_history_files(limit=10)
    lines = ["🧾 BETGPTAI WEIGHT HISTORY", ""]
    if not files:
        lines.append("No weight history saved yet.")
        return "\n".join(lines).strip()
    for path in reversed(files):
        payload = _read_json(path, {})
        changes = payload.get("changes_applied") if isinstance(payload, dict) else []
        lines.append(f"📅 {path.stem}")
        if changes:
            for item in changes[:8]:
                lines.append(
                    f"- {item.get('factor')}: {float(item.get('old_value', 0)):.4f} → "
                    f"{float(item.get('new_value', 0)):.4f} ({float(item.get('change', 0)):+.4f})"
                )
        else:
            lines.append("- No changes applied.")
        lines.append("")
    return "\n".join(lines).strip()


def render_auto_apply_notification(report: dict[str, Any]) -> str:
    """Render admin-only notification when auto-apply runs or is blocked."""
    result = report.get("auto_apply_result") if isinstance(report.get("auto_apply_result"), dict) else {}
    safety = result.get("safety_status") if isinstance(result.get("safety_status"), dict) else {}
    changes = result.get("applied_updates") if isinstance(result.get("applied_updates"), list) else []
    if not result.get("enabled"):
        return ""
    if not result.get("auto_applied"):
        reasons = safety.get("reasons") if isinstance(safety.get("reasons"), list) else []
        return (
            "🧠 AI Learning Auto-Apply Blocked\n"
            f"Date: {report.get('display_date')}\n"
            f"Safety Status: Failed\n"
            f"Reason: {'; '.join(reasons) if reasons else result.get('message', 'Blocked')}"
        )
    lines = [
        "🧠 AI Learning Applied",
        f"Date: {report.get('display_date')}",
        "Changes:",
    ]
    if changes:
        for item in changes:
            lines.append(
                f"- {item.get('factor')}: {float(item.get('old_value', 0)):.4f} → "
                f"{float(item.get('new_value', 0)):.4f} ({float(item.get('change', 0)):+.4f})"
            )
    else:
        lines.append("- None")
    lines.extend(
        [
            "Before: saved in history file",
            "After: model_weights.json updated",
            "Safety Status: Passed",
            f"Saved: {result.get('history_path', 'Unavailable')}",
        ]
    )
    return "\n".join(lines).strip()


def approve_weight_update() -> dict[str, Any]:
    """Owner approval entrypoint."""
    return approve_pending_weight_updates()


def reject_weight_update() -> dict[str, Any]:
    """Owner rejection entrypoint."""
    return clear_pending_weight_updates("rejected_by_owner")
