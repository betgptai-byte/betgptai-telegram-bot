"""Persist a small, secret-free audit of each generated MLB card."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


REPORT_FILE = Path(__file__).with_name("model_reports.json")
POSTING_LOG_FILE = Path(__file__).with_name("posting_log.json")
UNAVAILABLE_VALUES = (None, "", "unavailable", [], {})


def _has_data(value: Any) -> bool:
    """Recognize partial source payloads without counting unavailable shells."""
    if value in UNAVAILABLE_VALUES:
        return False
    if isinstance(value, dict):
        return any(_has_data(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_data(item) for item in value)
    return True


def _source_count(slate: list[dict[str, Any]], key: str) -> int:
    """Count games where an optional source returned usable structured data."""
    return sum(_has_data(game.get(key)) for game in slate)


def _savant_enriched(game: dict[str, Any]) -> bool:
    """A game is enriched when at least one Savant section contains metrics."""
    savant = game.get("savant")
    if not isinstance(savant, dict):
        return False
    return any(
        _has_data(savant.get(key))
        for key in ("away_pitcher", "home_pitcher", "away_team", "home_team",
                    "away_batters", "home_batters")
    )


def build_model_report(
    card_date: str, slate: list[dict[str, Any]], card: str,
    analysis_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize which inputs and analysts actually reached the final card."""
    analysis_metadata = analysis_metadata or {}
    total_games = len(slate)
    savant_games = sum(_savant_enriched(game) for game in slate)
    highlightly_games = _source_count(slate, "highlightly")
    fangraphs_games = _source_count(slate, "fangraphs")
    api_sports_baseball_games = _source_count(slate, "api_sports_baseball_context")
    weather_games = _source_count(slate, "weather")
    odds_games = sum(
        game.get("odds_status") == "available"
        and bool(game.get("best_available_prices"))
        for game in slate
    )
    openai_used = bool(analysis_metadata.get("openai_used"))
    claude_used = bool(analysis_metadata.get("claude_used"))
    consensus_count = int(analysis_metadata.get("consensus_picks_found") or 0)
    value_engine_count = int(analysis_metadata.get("value_engine_count") or 0)
    nrfi_candidates = int(analysis_metadata.get("nrfi_candidates") or 0)
    f5_candidates = int(analysis_metadata.get("f5_candidates") or 0)
    team_total_candidates = int(analysis_metadata.get("team_total_candidates") or 0)

    unavailable_sources = []
    if savant_games == 0:
        unavailable_sources.append("Baseball Savant")
    if highlightly_games == 0:
        unavailable_sources.append("Highlightly")
    if fangraphs_games == 0:
        unavailable_sources.append("FanGraphs")
    if api_sports_baseball_games == 0:
        unavailable_sources.append("API-Sports Baseball")
    if weather_games == 0:
        unavailable_sources.append("Weather")
    if odds_games == 0:
        unavailable_sources.append("Odds API")
    if not openai_used:
        unavailable_sources.append("OpenAI")
    if not claude_used:
        unavailable_sources.append("Claude")
    if analysis_metadata.get("fallback_used"):
        unavailable_sources.append("API-only card fallback")

    return {
        "date": card_date,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "total_games": total_games,
        "sources": {
            "mlb_stats": total_games > 0,
            "baseball_savant": savant_games > 0,
            "fangraphs": fangraphs_games > 0,
            "highlightly": highlightly_games > 0,
            "api_sports_baseball": api_sports_baseball_games > 0,
            "weather": weather_games > 0,
            "odds_api": odds_games > 0,
            "openai": openai_used,
            "claude": claude_used,
        },
        "savant_games_enriched": savant_games,
        "fangraphs_games_enriched": fangraphs_games,
        "api_sports_baseball_games_enriched": api_sports_baseball_games,
        "consensus_picks_found": consensus_count,
        "value_engine_count": value_engine_count,
        "nrfi_candidates": nrfi_candidates,
        "f5_candidates": f5_candidates,
        "team_total_candidates": team_total_candidates,
        "strikeout_candidates": int(analysis_metadata.get("strikeout_candidates") or 0),
        "home_run_candidates": int(analysis_metadata.get("home_run_candidates") or 0),
        "fallbacks_used": unavailable_sources,
        "auto_posting": auto_posting_status(card_date),
    }


def save_model_report(
    card_date: str, slate: list[dict[str, Any]], card: str,
    analysis_metadata: dict[str, Any] | None = None,
) -> None:
    """Atomically save reports by sports date so restarts retain the audit."""
    reports: dict[str, Any] = {}
    if REPORT_FILE.exists():
        try:
            payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                reports = payload
        except (OSError, ValueError):
            reports = {}
    reports[card_date] = build_model_report(card_date, slate, card, analysis_metadata)
    temporary = REPORT_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    temporary.replace(REPORT_FILE)


def load_model_report(card_date: str) -> dict[str, Any] | None:
    """Load one sports day's report, returning None when none was generated."""
    try:
        payload = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    report = payload.get(card_date) if isinstance(payload, dict) else None
    return report if isinstance(report, dict) else None


def auto_posting_status(card_date: str) -> dict[str, Any]:
    """Summarize daily scheduler delivery from posting_log.json."""
    try:
        payload = json.loads(POSTING_LOG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {"status": "No posting log found", "sent": 0, "skipped": 0, "pending": "unknown"}
    day = payload.get(card_date, {}) if isinstance(payload, dict) else {}
    if not isinstance(day, dict):
        return {"status": "No posting log found", "sent": 0, "skipped": 0, "pending": "unknown"}
    jobs = {
        key: value for key, value in day.items()
        if isinstance(value, dict) and key != "_meta" and "_live_" not in key
    }
    sent = sum(1 for value in jobs.values() if value.get("status") == "sent")
    skipped = sum(1 for value in jobs.values() if value.get("status") == "skipped")
    failed = sum(1 for value in jobs.values() if value.get("status") == "failed")
    status = "Active" if jobs else "Waiting for due posts"
    return {
        "status": status,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "last_recorded_at": max(
            (str(value.get("recorded_at")) for value in jobs.values() if value.get("recorded_at")),
            default="Unavailable",
        ),
    }
