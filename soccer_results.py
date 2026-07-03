"""Read and format the owner-only BETGPTAI soccer results dashboard."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from card_format import RECOMMENDATION_FOOTER


RESULTS_FILE = Path(__file__).resolve().parent / "soccer_results.json"
CATEGORIES = (
    ("overall", "Overall"),
    ("moneyline", "Moneyline / 1X2"),
    ("btts", "BTTS"),
    ("totals", "Over/Under"),
    ("corners", "Corners"),
    ("parlays", "Parlays"),
)


class SoccerResultsError(Exception):
    """Raised when an existing soccer results file contains invalid data."""


def load_soccer_results() -> dict[str, Any]:
    """Load tracked results, returning a clean zero dashboard when absent."""
    if not RESULTS_FILE.exists():
        return {"last_updated": "Unavailable"}
    try:
        payload = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SoccerResultsError("soccer_results.json could not be read.") from error
    if not isinstance(payload, dict):
        raise SoccerResultsError("soccer_results.json must contain an object.")
    return payload


def build_soccer_results_dashboard() -> str:
    """Create a compact owner dashboard without inventing unsettled results."""
    results = load_soccer_results()
    sections = []
    for key, label in CATEGORIES:
        record = results.get(key, {})
        if not isinstance(record, dict):
            record = {}
        wins = int(record.get("wins", 0) or 0)
        losses = int(record.get("losses", 0) or 0)
        pushes = int(record.get("pushes", 0) or 0)
        graded = wins + losses
        win_percentage = round(100 * wins / graded, 1) if graded else 0
        profit = float(record.get("profit_units", 0) or 0)
        sections.append(
            f"{label}:\n"
            f"W-L-P: {wins}-{losses}-{pushes}\n"
            f"Win %: {win_percentage:g}%\n"
            f"Profit Units: {profit:+g}"
        )
    return (
        "⚽ BETGPTAI SOCCER RESULTS\n\n"
        + "\n\n".join(sections)
        + f"\n\nLast Updated: {results.get('last_updated', 'Unavailable')}\n\n"
        + RECOMMENDATION_FOOTER
    )
