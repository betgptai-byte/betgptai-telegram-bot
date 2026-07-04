"""BETGPTAI model-weight storage and approval utilities.

Phase 6 is learning-review mode only. Suggested updates are saved separately
and never applied until the owner runs /approve_weight_update.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import data_file


EASTERN = ZoneInfo("America/New_York")
MODEL_WEIGHTS_FILE = data_file("model_weights.json")
PENDING_WEIGHT_UPDATES_FILE = data_file("pending_weight_updates.json")
LEARNING_REPORTS_DIR = data_file("learning_reports")
MAX_DAILY_ADJUSTMENT = 0.05
MAX_WEEKLY_ADJUSTMENT = 0.15


DEFAULT_WEIGHTS = {
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


def approve_pending_weight_updates() -> dict[str, Any]:
    """Apply pending updates while respecting daily/weekly caps."""
    pending = load_pending_weight_updates()
    suggestions = pending.get("suggestions") if isinstance(pending.get("suggestions"), dict) else {}
    if not suggestions:
        return {"applied": 0, "message": "No pending weight updates found."}

    payload = load_model_weights_payload()
    weights = ensure_model_weights()
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    applied: list[dict[str, Any]] = []
    skipped: list[str] = []
    for factor, item in suggestions.items():
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
        weekly_used = _weekly_adjustment_used(history, factor)
        remaining = max(0.0, MAX_WEEKLY_ADJUSTMENT - weekly_used)
        if remaining <= 0:
            skipped.append(f"{factor}: weekly cap reached")
            continue
        if abs(change) > remaining:
            change = remaining if change > 0 else -remaining
        old_value = weights[factor]
        new_value = max(0.0, min(1.0, old_value + change))
        actual_change = round(new_value - old_value, 4)
        if actual_change == 0:
            skipped.append(f"{factor}: no effective change")
            continue
        weights[factor] = round(new_value, 4)
        applied.append(
            {
                "factor": factor,
                "old_value": old_value,
                "new_value": weights[factor],
                "change": actual_change,
                "reason": item.get("reason", "") if isinstance(item, dict) else "",
                "applied_at": _now_iso(),
            }
        )

    history.extend(applied)
    _write_json(
        MODEL_WEIGHTS_FILE,
        {
            "weights": weights,
            "updated_at": _now_iso(),
            "auto_apply": False,
            "history": history[-250:],
        },
    )
    _write_json(PENDING_WEIGHT_UPDATES_FILE, {})
    return {
        "applied": len(applied),
        "applied_updates": applied,
        "skipped": skipped,
        "message": "Pending weight updates applied with caps.",
    }


def learning_reports_count() -> int:
    """Count saved learning report JSON files."""
    LEARNING_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    return len(list(LEARNING_REPORTS_DIR.glob("*.json")))
