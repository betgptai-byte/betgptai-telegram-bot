"""BETGPTAI model-weight storage, approval, and safe auto-apply utilities.

Manual owner approval remains supported. When AI_LEARNING_AUTO_APPLY is enabled,
the same suggestions can be applied automatically only after strict safety gates
pass. These helpers are admin/internal only and never expose model details to
members.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import data_file


EASTERN = ZoneInfo("America/New_York")
MODEL_WEIGHTS_FILE = data_file("model_weights.json")
PENDING_WEIGHT_UPDATES_FILE = data_file("pending_weight_updates.json")
LEARNING_REPORTS_DIR = data_file("learning_reports")
LEARNING_AUTO_SETTINGS_FILE = data_file("learning_auto_apply.json")
MODEL_WEIGHT_HISTORY_DIR = data_file("model_weights") / "history"
MAX_DAILY_ADJUSTMENT = 0.05
MAX_DAILY_TOTAL_MOVEMENT = 0.15
MAX_WEEKLY_ADJUSTMENT = 0.15
MIN_GRADED_PICKS_FOR_AUTO_APPLY = 5
MIN_LOSS_REASON_CONFIDENCE = 70.0
WEIGHT_FLOOR = 0.0
WEIGHT_CEILING = 1.0


DEFAULT_WEIGHTS = {
    # Quant Engine v21 component weights.
    "sp_score": 0.25,
    "bullpen_score": 0.15,
    "sp_batter_matchup_score": 0.10,
    "weather_park_score": 0.10,
    "market_value_score": 0.10,
    "offense_score": 0.10,
    "situational_score": 0.10,
    "defense_score": 0.05,
    "home_away_score": 0.025,
    "travel_rest_score": 0.025,
    "recent_form_score": 0.025,
    "team_splits_score": 0.025,
    "starting_pitcher_edge": 0.50,
    "bullpen_edge": 0.50,
    "lineup_confirmation": 0.50,
    "weather_edge": 0.50,
    "park_factor": 0.50,
    "recent_form": 0.50,
    "statcast_contact": 0.50,
    "pitch_type_matchup": 0.50,
    "bvp": 0.25,
    "travel_rest": 0.35,
    "market_value": 0.50,
    "team_offense_vs_handedness": 0.50,
    "bullpen_fatigue": 0.50,
    "player_streaks": 0.45,
    "player_lineup_spot": 0.50,
    "player_team_verification": 0.70,
}

QUANT_WEIGHT_KEYS = (
    "sp_score",
    "bullpen_score",
    "sp_batter_matchup_score",
    "weather_park_score",
    "market_value_score",
    "offense_score",
    "situational_score",
    "defense_score",
    "home_away_score",
    "travel_rest_score",
    "recent_form_score",
    "team_splits_score",
)

LEARNING_TO_QUANT_ALIASES = {
    "starting_pitcher_edge": "sp_score",
    "team_offense_vs_handedness": "offense_score",
    "recent_form": "offense_score",
    "statcast_contact": "offense_score",
    "bullpen_edge": "bullpen_score",
    "bullpen_fatigue": "bullpen_score",
    "weather_edge": "weather_park_score",
    "park_factor": "weather_park_score",
    "market_value": "market_value_score",
    "travel_rest": "situational_score",
    "lineup_confirmation": "offense_score",
    "pitch_type_matchup": "offense_score",
    "bvp": "offense_score",
    "player_streaks": "offense_score",
    "player_lineup_spot": "offense_score",
    "player_team_verification": "offense_score",
}


def _now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


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


def ensure_model_weights() -> dict[str, float]:
    """Create and return model_weights.json with safe defaults."""
    payload = _read_json(MODEL_WEIGHTS_FILE, {})
    if not isinstance(payload, dict):
        payload = {}
    weights = dict(DEFAULT_WEIGHTS)
    stored_weights = payload.get("weights") if isinstance(payload.get("weights"), dict) else payload
    if isinstance(stored_weights, dict):
        for key, value in stored_weights.items():
            if key in weights:
                try:
                    weights[key] = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    pass
    if not MODEL_WEIGHTS_FILE.exists() or payload.get("weights") != weights:
        _write_json(
            MODEL_WEIGHTS_FILE,
            {
                "weights": weights,
                "updated_at": _now_iso(),
                "auto_apply": False,
                "history": payload.get("history", []) if isinstance(payload.get("history"), list) else [],
            },
        )
    return weights


def load_model_weights() -> dict[str, float]:
    """Return current model weights, creating defaults when needed."""
    return ensure_model_weights()


def load_quant_engine_weights() -> dict[str, float]:
    """Return v20 component weights from model_weights.json on every score.

    If the file is missing, safe defaults are created. If extra learning-factor
    weights exist, they remain available for diagnostics while the quant engine
    consumes only the component keys it understands.
    """
    weights = ensure_model_weights()
    return {key: float(weights.get(key, DEFAULT_WEIGHTS[key])) for key in QUANT_WEIGHT_KEYS}


def load_model_weights_payload() -> dict[str, Any]:
    """Return the full model weight payload."""
    ensure_model_weights()
    payload = _read_json(MODEL_WEIGHTS_FILE, {})
    return payload if isinstance(payload, dict) else {"weights": dict(DEFAULT_WEIGHTS)}


def load_pending_weight_updates() -> dict[str, Any]:
    """Return pending owner-approval suggestions."""
    payload = _read_json(PENDING_WEIGHT_UPDATES_FILE, {})
    return payload if isinstance(payload, dict) else {}


def save_pending_weight_updates(card_date: str, suggestions: dict[str, Any]) -> None:
    """Save pending suggestions for owner approval."""
    payload = {
        "card_date": card_date,
        "created_at": _now_iso(),
        "auto_apply": False,
        "status": "pending_admin_approval",
        "suggestions": suggestions,
    }
    _write_json(PENDING_WEIGHT_UPDATES_FILE, payload)


def _env_auto_apply_default() -> bool:
    return str(os.getenv("AI_LEARNING_AUTO_APPLY", "false")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def ai_learning_auto_apply_enabled() -> bool:
    """Return the current AI Learning auto-apply setting.

    Runtime owner toggles are stored in DATA_DIR. If no override exists, the
    Railway/local environment variable is used.
    """
    payload = _read_json(LEARNING_AUTO_SETTINGS_FILE, {})
    if isinstance(payload, dict) and isinstance(payload.get("enabled"), bool):
        return bool(payload["enabled"])
    return _env_auto_apply_default()


def set_ai_learning_auto_apply(enabled: bool) -> dict[str, Any]:
    """Persist the owner-controlled auto-apply toggle."""
    payload = {
        "enabled": bool(enabled),
        "updated_at": _now_iso(),
        "source": "owner_command",
    }
    _write_json(LEARNING_AUTO_SETTINGS_FILE, payload)
    return payload


def toggle_ai_learning_auto_apply() -> dict[str, Any]:
    """Flip the owner-controlled auto-apply toggle."""
    return set_ai_learning_auto_apply(not ai_learning_auto_apply_enabled())


def clear_pending_weight_updates(status: str = "rejected") -> dict[str, Any]:
    """Clear pending suggestions without applying them."""
    pending = load_pending_weight_updates()
    archive = {
        "cleared_at": _now_iso(),
        "status": status,
        "previous": pending,
    }
    _write_json(PENDING_WEIGHT_UPDATES_FILE, {})
    return archive


def _weekly_adjustment_used(history: list[dict[str, Any]], key: str) -> float:
    cutoff = datetime.now(EASTERN) - timedelta(days=7)
    used = 0.0
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("factor") != key:
            continue
        try:
            applied_at = datetime.fromisoformat(str(item.get("applied_at")))
        except ValueError:
            continue
        if applied_at.tzinfo is None:
            applied_at = applied_at.replace(tzinfo=EASTERN)
        if applied_at >= cutoff:
            used += abs(float(item.get("change", 0)))
    return used


def _history_files_for_last_week(card_date: str) -> list[Path]:
    try:
        target = datetime.fromisoformat(card_date)
    except ValueError:
        target = datetime.now(EASTERN)
    start = target - timedelta(days=6)
    MODEL_WEIGHT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for index in range(7):
        day = (start + timedelta(days=index)).date().isoformat()
        path = MODEL_WEIGHT_HISTORY_DIR / f"{day}.json"
        if path.exists():
            files.append(path)
    return files


def _weekly_factor_movement_from_history(card_date: str, factor: str) -> float:
    used = 0.0
    for path in _history_files_for_last_week(card_date):
        payload = _read_json(path, {})
        changes = payload.get("changes_applied") if isinstance(payload, dict) else []
        if not isinstance(changes, list):
            continue
        for item in changes:
            if not isinstance(item, dict) or item.get("factor") != factor:
                continue
            try:
                used += abs(float(item.get("change", 0)))
            except (TypeError, ValueError):
                continue
    return round(used, 4)


def _suggestions_with_quant_aliases(suggestions: dict[str, Any]) -> dict[str, Any]:
    """Copy suggestions and mirror learning factors into v20 component keys."""
    expanded = dict(suggestions)
    for factor, item in suggestions.items():
        alias = LEARNING_TO_QUANT_ALIASES.get(factor)
        if not alias:
            continue
        raw_change = item.get("suggested_change") if isinstance(item, dict) else item
        try:
            change = float(raw_change)
        except (TypeError, ValueError):
            continue
        target = expanded.setdefault(
            alias,
            {
                "suggested_change": 0.0,
                "reason_tags": [],
                "reason": "Mirrored Phase 6 learning factor into v20 component weight.",
            },
        )
        if isinstance(target, dict):
            target["suggested_change"] = float(target.get("suggested_change", 0)) + change
            reason_tags = target.setdefault("reason_tags", [])
            if isinstance(item, dict):
                reason_tags.extend(item.get("reason_tags", []))
    return expanded


def _reason_tags_from_suggestions(suggestions: dict[str, Any]) -> list[str]:
    tags: set[str] = set()
    for item in suggestions.values():
        if isinstance(item, dict):
            tags.update(str(tag) for tag in item.get("reason_tags", []) if tag)
    return sorted(tags)


def _loss_reason_confidence(report: dict[str, Any]) -> float:
    losses = report.get("reviewed_losses") if isinstance(report.get("reviewed_losses"), list) else []
    confidences: list[float] = []
    for item in losses:
        if not isinstance(item, dict):
            continue
        try:
            confidences.append(float(item.get("loss_reason_confidence")))
        except (TypeError, ValueError):
            pass
    if confidences:
        return min(confidences)
    try:
        return float(report.get("loss_reason_confidence"))
    except (TypeError, ValueError):
        return 0.0


def validate_auto_apply_safety(report: dict[str, Any], suggestions: dict[str, Any]) -> dict[str, Any]:
    """Return safety-gate details for AI Learning auto-apply."""
    reviewed = int(report.get("losses_reviewed") or 0) + int(report.get("wins_reviewed") or 0)
    confidence = _loss_reason_confidence(report)
    reasons: list[str] = []
    if reviewed < MIN_GRADED_PICKS_FOR_AUTO_APPLY:
        reasons.append(
            f"Only {reviewed} graded picks reviewed; minimum is {MIN_GRADED_PICKS_FOR_AUTO_APPLY}."
        )
    if confidence < MIN_LOSS_REASON_CONFIDENCE:
        reasons.append(
            f"Loss reason confidence {confidence:.0f}% is below {MIN_LOSS_REASON_CONFIDENCE:.0f}%."
        )
    if not suggestions:
        reasons.append("No suggested weight updates available.")
    return {
        "passed": not reasons,
        "reviewed_picks": reviewed,
        "loss_reason_confidence": confidence,
        "reasons": reasons,
        "limits": {
            "max_single_factor_change_per_day": MAX_DAILY_ADJUSTMENT,
            "max_total_model_movement_per_day": MAX_DAILY_TOTAL_MOVEMENT,
            "max_single_factor_movement_per_7_days": MAX_WEEKLY_ADJUSTMENT,
            "weight_floor": WEIGHT_FLOOR,
            "weight_ceiling": WEIGHT_CEILING,
            "min_graded_picks_reviewed": MIN_GRADED_PICKS_FOR_AUTO_APPLY,
            "min_loss_reason_confidence": MIN_LOSS_REASON_CONFIDENCE,
        },
    }


def apply_weight_updates_safely(
    *,
    card_date: str,
    suggestions: dict[str, Any],
    learning_report_path: str | None = None,
    auto_applied: bool = False,
    safety_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply suggestions with all daily, weekly, floor, and ceiling limits."""
    payload = load_model_weights_payload()
    old_weights = ensure_model_weights()
    weights = dict(old_weights)
    expanded = _suggestions_with_quant_aliases(suggestions)
    applied: list[dict[str, Any]] = []
    skipped: list[str] = []
    daily_total = 0.0

    for factor, item in expanded.items():
        if factor not in weights:
            skipped.append(f"{factor}: unknown factor")
            continue
        raw_change = item.get("suggested_change") if isinstance(item, dict) else item
        try:
            change = float(raw_change)
        except (TypeError, ValueError):
            skipped.append(f"{factor}: invalid change")
            continue
        change = max(-MAX_DAILY_ADJUSTMENT, min(MAX_DAILY_ADJUSTMENT, change))
        remaining_daily = max(0.0, MAX_DAILY_TOTAL_MOVEMENT - daily_total)
        if remaining_daily <= 0:
            skipped.append(f"{factor}: daily total movement cap reached")
            continue
        if abs(change) > remaining_daily:
            change = remaining_daily if change > 0 else -remaining_daily

        weekly_used = _weekly_factor_movement_from_history(card_date, factor)
        remaining_weekly = max(0.0, MAX_WEEKLY_ADJUSTMENT - weekly_used)
        if remaining_weekly <= 0:
            skipped.append(f"{factor}: 7-day factor cap reached")
            continue
        if abs(change) > remaining_weekly:
            change = remaining_weekly if change > 0 else -remaining_weekly

        old_value = float(weights[factor])
        new_value = max(WEIGHT_FLOOR, min(WEIGHT_CEILING, old_value + change))
        actual_change = round(new_value - old_value, 4)
        if actual_change == 0:
            skipped.append(f"{factor}: no effective change")
            continue
        weights[factor] = round(new_value, 4)
        daily_total += abs(actual_change)
        applied.append(
            {
                "factor": factor,
                "old_value": round(old_value, 4),
                "new_value": weights[factor],
                "change": actual_change,
                "reason": item.get("reason", "") if isinstance(item, dict) else "",
                "reason_tags": item.get("reason_tags", []) if isinstance(item, dict) else [],
                "applied_at": _now_iso(),
            }
        )

    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    history.extend(applied)
    _write_json(
        MODEL_WEIGHTS_FILE,
        {
            "weights": weights,
            "updated_at": _now_iso(),
            "auto_apply": bool(auto_applied),
            "history": history[-250:],
        },
    )

    history_path = MODEL_WEIGHT_HISTORY_DIR / f"{card_date}.json"
    history_payload = {
        "old_weights": old_weights,
        "new_weights": weights,
        "changes_applied": applied,
        "learning_report_used": learning_report_path,
        "reason_tags": _reason_tags_from_suggestions(expanded),
        "applied_at": _now_iso(),
        "auto_applied": bool(auto_applied),
        "safety_status": safety_report or {},
        "skipped": skipped,
    }
    _write_json(history_path, history_payload)
    return {
        "applied": len(applied),
        "applied_updates": applied,
        "skipped": skipped,
        "history_path": str(history_path),
        "old_weights": old_weights,
        "new_weights": weights,
        "safety_status": safety_report or {},
        "message": "Weight updates applied with safety caps.",
    }


def maybe_auto_apply_weight_updates(report: dict[str, Any]) -> dict[str, Any]:
    """Apply learning suggestions automatically only when every gate passes."""
    if not ai_learning_auto_apply_enabled():
        return {"enabled": False, "applied": 0, "message": "AI Learning Auto Apply is OFF."}

    suggestions = report.get("suggested_adjustments") if isinstance(report.get("suggested_adjustments"), dict) else {}
    safety = validate_auto_apply_safety(report, suggestions)
    if not safety.get("passed"):
        return {
            "enabled": True,
            "applied": 0,
            "safety_status": safety,
            "message": "AI Learning Auto Apply blocked by safety limits.",
        }
    result = apply_weight_updates_safely(
        card_date=str(report.get("card_date") or datetime.now(EASTERN).date().isoformat()),
        suggestions=suggestions,
        learning_report_path=str(report.get("report_path") or ""),
        auto_applied=True,
        safety_report=safety,
    )
    result["enabled"] = True
    result["auto_applied"] = True
    result["message"] = "AI Learning Auto Apply completed safely."
    _write_json(PENDING_WEIGHT_UPDATES_FILE, {})
    return result


def approve_pending_weight_updates() -> dict[str, Any]:
    """Apply pending updates while respecting daily/weekly caps."""
    pending = load_pending_weight_updates()
    suggestions = pending.get("suggestions") if isinstance(pending.get("suggestions"), dict) else {}
    if not suggestions:
        return {"applied": 0, "message": "No pending weight updates found."}
    result = apply_weight_updates_safely(
        card_date=str(pending.get("card_date") or datetime.now(EASTERN).date().isoformat()),
        suggestions=suggestions,
        learning_report_path=str(LEARNING_REPORTS_DIR / f"{pending.get('card_date', '')}.json"),
        auto_applied=False,
        safety_report={"manual_approval": True},
    )
    _write_json(PENDING_WEIGHT_UPDATES_FILE, {})
    result["message"] = "Pending weight updates applied with caps."
    return result


def learning_reports_count() -> int:
    """Count saved learning report JSON files."""
    LEARNING_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return len(list(LEARNING_REPORTS_DIR.glob("*.json")))


def latest_weight_history() -> dict[str, Any]:
    """Return the newest model-weight history payload."""
    MODEL_WEIGHT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(MODEL_WEIGHT_HISTORY_DIR.glob("*.json"))
    if not files:
        return {}
    payload = _read_json(files[-1], {})
    if isinstance(payload, dict):
        payload["history_path"] = str(files[-1])
        return payload
    return {}


def weight_history_files(limit: int = 10) -> list[Path]:
    """Return recent model-weight history files."""
    MODEL_WEIGHT_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(MODEL_WEIGHT_HISTORY_DIR.glob("*.json"))[-limit:]
