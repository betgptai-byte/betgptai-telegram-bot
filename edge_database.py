"""BETGPTAI v20 edge database utilities.

This module keeps the engine deterministic and auditable. It does not invent
stats; it only stores calculated edge outputs from verified slate data.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from storage import data_file
from model_weights import load_quant_engine_weights


MODEL_VERSION = "BETGPTAI v20.0"
WEIGHTS = {
    "sp_score": 0.30,
    "offense_score": 0.20,
    "bullpen_score": 0.15,
    "defense_score": 0.10,
    "weather_park_score": 0.10,
    "market_value_score": 0.10,
    "situational_score": 0.05,
}
MINIMUM_EDGE_SCORE = 82.0


def clamp(value: Any, low: float = 0.0, high: float = 100.0) -> float:
    """Clamp a numeric value to a score range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def current_quant_weights() -> dict[str, float]:
    """Load current v20 component weights from DATA_DIR/model_weights.json."""
    try:
        return load_quant_engine_weights()
    except Exception:
        return dict(WEIGHTS)


def weighted_score(scores: dict[str, float]) -> float:
    """Calculate the v20 weighted final edge score."""
    weights = current_quant_weights()
    return round(sum(clamp(scores.get(key, 0)) * weight for key, weight in weights.items()), 2)


def confidence_from_score(score: float) -> str:
    """Map edge score to a simple confidence tier."""
    if score >= 92:
        return "Elite"
    if score >= 87:
        return "Strong"
    if score >= 82:
        return "Playable"
    return "PASS"


def risk_level(score: float, data_quality_grade: str) -> str:
    """Return a conservative risk label."""
    if score < MINIMUM_EDGE_SCORE:
        return "PASS"
    if data_quality_grade in {"D", "F"}:
        return "High"
    if score >= 90:
        return "Moderate"
    return "Medium"


def data_quality_grade(available: int, possible: int) -> str:
    """Grade how complete the verified data was."""
    ratio = available / possible if possible else 0
    if ratio >= 0.85:
        return "A"
    if ratio >= 0.70:
        return "B"
    if ratio >= 0.55:
        return "C"
    if ratio >= 0.40:
        return "D"
    return "F"


def save_edge_snapshot(card_date: str, payload: list[dict[str, Any]]) -> str:
    """Persist calculated v20 edges for owner diagnostics and learning."""
    path = data_file("edge_database") / f"{card_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "model_version": MODEL_VERSION,
            "card_date": card_date,
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "games": payload,
        }, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return str(path)
