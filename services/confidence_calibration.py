"""BETGPTAI Confidence Calibration — adjusts edge scores based on historical accuracy.

Tracks actual win rate per edge bucket, then produces a calibrated confidence score
using Bayesian shrinkage toward the model's stated edge.

Data stored at: data/confidence_calibration/calibration.json
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from storage import data_file

logger = logging.getLogger(__name__)

CALIBRATION_DIR = data_file("confidence_calibration")
CALIBRATION_FILE = CALIBRATION_DIR / "calibration.json"

BUCKET_SIZE = 5


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        if path.exists():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed reading %s: %s", path, exc)
    return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _load_calibration_data() -> dict[str, Any]:
    data = _read_json(CALIBRATION_FILE, {})
    if isinstance(data, dict):
        return data
    return {}


def _save_calibration_data(data: dict[str, Any]) -> None:
    _write_json(CALIBRATION_FILE, data)


def _bucket_key(edge: float) -> str:
    lo = int(edge // BUCKET_SIZE) * BUCKET_SIZE
    hi = lo + BUCKET_SIZE
    return f"{lo}-{hi}"


def record_result(edge_score: float, result: str) -> None:
    """Record a graded pick result in the calibration data."""
    if result not in ("win", "loss", "push"):
        return
    data = _load_calibration_data()
    ext = data.setdefault("buckets", {})
    bucket = _bucket_key(edge_score)
    b = ext.setdefault(bucket, {"wins": 0, "losses": 0, "pushes": 0, "total": 0})
    b[result] = b.get(result, 0) + 1
    b["total"] = b.get("total", 0) + 1
    data["last_updated"] = datetime.utcnow().isoformat()
    _save_calibration_data(data)


def calibrate_confidence(edge_score: float) -> float:
    """Returns calibrated confidence 0–100 using Bayesian shrinkage.

    Blends the observed win rate in the bucket with the theoretical edge.
    Prior strength = 20 (minimum observations before trusting data fully).
    """
    data = _load_calibration_data()
    buckets = _dict(data.get("buckets"))
    bucket = _bucket_key(edge_score)
    b = _dict(buckets.get(bucket))
    wins = b.get("wins", 0)
    total = b.get("total", 0)
    if total == 0:
        return edge_score
    observed_rate = wins / total
    prior = edge_score / 100.0
    prior_strength = 20
    calibrated = (prior * prior_strength + observed_rate * total) / (prior_strength + total)
    return round(calibrated * 100, 1)


def calibration_summary() -> list[dict[str, Any]]:
    """Render a summary of calibration data per bucket."""
    data = _load_calibration_data()
    buckets_raw = _dict(data.get("buckets"))
    summary = []
    for bucket in sorted(buckets_raw, key=lambda x: int(x.split("-")[0])):
        b = buckets_raw[bucket]
        wins = b.get("wins", 0)
        total = b.get("total", 0)
        observed_pct = round((wins / total) * 100, 1) if total > 0 else 0.0
        mid = (int(bucket.split("-")[0]) + int(bucket.split("-")[1])) / 2
        calibrated = calibrate_confidence(mid)
        summary.append({
            "bucket": bucket,
            "wins": wins,
            "losses": b.get("losses", 0),
            "pushes": b.get("pushes", 0),
            "total": total,
            "observed_win_rate": observed_pct,
            "model_edge": mid,
            "calibrated_edge": calibrated,
            "delta": round(calibrated - mid, 1),
        })
    return summary


def render_confidence_debug() -> str:
    """Render confidence calibration debug output."""
    summary = calibration_summary()
    lines = [
        "🎯 BETGPTAI CONFIDENCE CALIBRATION",
        f"Last updated: {_load_calibration_data().get('last_updated', 'never')}",
        "",
        f"{'Bucket':<10} {'Wins':>5} {'Losses':>5} {'Total':>5} {'Obs%':>6} {'Model':>6} {'Calib':>6} {'Delta':>6}",
        "─" * 55,
    ]
    for s in summary:
        lines.append(
            f"{s['bucket']:<10} {s['wins']:>5} {s['losses']:>5} {s['total']:>5} "
            f"{s['observed_win_rate']:>5.1f}% {s['model_edge']:>5.0f} "
            f"{s['calibrated_edge']:>5.0f} {s['delta']:>+5.1f}"
        )
    if not summary:
        lines.append("No calibration data yet — results will populate after grading.")
    return "\n".join(lines).strip()
