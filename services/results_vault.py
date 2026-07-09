"""BETGPTAI Results Vault — immutable daily results records.

Loads the Daily Snapshot, grades every official pick using the existing
grading pipeline (``grade_mlb_picks_for_date``), and saves permanent results
to ``/data/results/YYYY/MM/YYYY-MM-DD_results.json``.  Never modifies the
snapshot.  Always grades from the immutable snapshot, never from regenerated
data.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import DATA_DIR, data_file
from services.daily_snapshot import load_snapshot, snapshot_status

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
RESULTS_ROOT = DATA_DIR / "results"
VAULT_LOG = DATA_DIR / "logs" / "vault.log"
VAULT_LOG.parent.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def _today_str() -> str:
    return datetime.now(EASTERN).strftime("%Y-%m-%d")


def _yesterday_str() -> str:
    return (datetime.now(EASTERN) - timedelta(days=1)).strftime("%Y-%m-%d")


def _log_vault(event: str, card_date: str, details: str = "") -> None:
    payload = {"timestamp": _now_iso(), "event": event, "card_date": card_date, "details": details}
    VAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with VAULT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _results_path(card_date: str) -> Path:
    parts = card_date.split("-")
    if len(parts) == 3:
        year, month = parts[0], parts[1]
    else:
        year, month = card_date[:4], card_date[5:7]
    return RESULTS_ROOT / year / month / f"{card_date}_results.json"


def _results_exist(card_date: str) -> bool:
    return _results_path(card_date).exists()


def _american_odds(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _units_won(result: str, odds: Any, units: Any) -> float:
    try:
        u = float(units or 1)
    except (TypeError, ValueError):
        u = 1.0
    if result == "win":
        odds_float = _american_odds(odds)
        if odds_float > 0:
            return round(u * odds_float / 100, 2)
        else:
            return round(u * 100 / abs(odds_float), 2)
    elif result == "loss":
        return -u
    return 0.0


# ── Grade from snapshot ───────────────────────────────────────────────────

def _market_from_pick(pick: dict[str, Any]) -> str:
    mt = str(pick.get("market_type") or pick.get("market") or "").lower()
    if mt in {"moneyline", "h2h", "ml"}:
        return "moneyline"
    if mt in {"runline", "spreads", "rl"}:
        return "runline"
    if mt in {"f5_moneyline", "f5"}:
        return "f5"
    if mt in {"total", "game_total", "totals"}:
        return "game_total"
    if mt in {"team_total", "team_totals", "tt"}:
        return "team_total"
    if mt == "parlay":
        return "parlay"
    if mt in {"play_of_day", "play_of_the_day"}:
        return "play_of_day"
    if "prop" in mt:
        return "prop"
    return mt


def _is_parlay_leg(pick: dict[str, Any]) -> bool:
    return str(pick.get("category") or "").lower() == "parlay_leg"


def grade_snapshot_date(card_date: str) -> dict[str, Any]:
    """Grade all official picks for one date using the existing grading pipeline."""
    snapshot = load_snapshot(card_date)
    if not snapshot:
        return {"success": False, "error": "No official snapshot saved for this date."}

    picks = _list(snapshot.get("official_picks"))
    if not picks:
        return {"success": False, "error": "No official picks in snapshot."}

    try:
        from results_tracker import grade_mlb_picks_for_date
        summary = grade_mlb_picks_for_date(card_date)
    except Exception as error:
        logger.exception("Grading failed for %s", card_date)
        _log_vault("grading_failed", card_date, repr(error))
        return {"success": False, "error": repr(error)}

    # Reload picks after grading
    try:
        from results_tracker import load_picks
        all_picks = load_picks()
    except Exception:
        all_picks = []

    todays = [p for p in all_picks if isinstance(p, dict) and str(p.get("card_date") or p.get("date") or "") == card_date and not _is_parlay_leg(p)]

    record = _build_results_record(card_date, todays, snapshot)
    path = _results_path(card_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    _log_vault("results_saved", card_date, str(path))

    try:
        _run_ai_learning(card_date)
    except Exception as error:
        logger.exception("AI learning after vault grading failed for %s", card_date)
        _log_vault("ai_learning_failed", card_date, repr(error))

    record["grading_summary"] = summary
    return {"success": True, "path": str(path), "record": record}


def _build_results_record(card_date: str, graded_picks: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Build the permanent results record from graded picks."""
    market_groups: dict[str, list[dict[str, Any]]] = {}
    for pick in graded_picks:
        market = _market_from_pick(pick)
        market_groups.setdefault(market, []).append(pick)

    results_summary: dict[str, Any] = {}
    total_units = 0.0
    total_wins = 0
    total_losses = 0
    total_pushes = 0
    pending_count = 0
    manual_review: list[str] = []
    pending_list: list[str] = []

    for market, market_picks in market_groups.items():
        wins = sum(1 for p in market_picks if p.get("result") == "win")
        losses = sum(1 for p in market_picks if p.get("result") == "loss")
        pushes = sum(1 for p in market_picks if p.get("result") == "push")
        pending = sum(1 for p in market_picks if p.get("result") not in ("win", "loss", "push"))
        units = sum(_units_won(p.get("result"), p.get("odds"), p.get("units") or p.get("units_risked", 1)) for p in market_picks)
        total_wins += wins
        total_losses += losses
        total_pushes += pushes
        pending_count += pending
        total_units += units

        results_summary[market] = {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "units": round(units, 2),
            "picks": [
                {
                    "pick_id": p.get("pick_id"),
                    "selection": p.get("pick_text") or p.get("selected_team"),
                    "market_type": _market_from_pick(p),
                    "line": p.get("market_line") or p.get("line"),
                    "odds": p.get("odds"),
                    "units": p.get("units") or p.get("units_risked", 1),
                    "edge_score": p.get("edge_score") or p.get("final_edge_score"),
                    "confidence": p.get("confidence") or p.get("confidence_grade"),
                    "result": p.get("result", "pending"),
                    "profit_units": round(_units_won(p.get("result"), p.get("odds"), p.get("units") or p.get("units_risked", 1)), 2),
                    "reason": p.get("reason"),
                }
                for p in market_picks
            ],
        }
        if market == "parlay":
            for p in market_picks:
                if p.get("result") not in ("win", "loss", "push"):
                    pending_list.append(f"{p.get('pick_text')} — {p.get('result', 'pending')}")
                legs = _list(p.get("legs"))
                results_summary[market]["legs"] = [
                    {
                        "leg_id": leg.get("pick_id"),
                        "selection": leg.get("pick_text") or leg.get("selected_team"),
                        "result": leg.get("result", "pending"),
                    }
                    for leg in legs
                ]

    total = total_wins + total_losses + total_pushes
    roi = round((total_units / total) * 100, 1) if total > 0 else 0.0

    # Check for manual review conditions
    if any(p.get("result") not in ("win", "loss", "push") for p in graded_picks):
        pending_examples = [
            f"{p.get('pick_text')} — {p.get('last_grading_error', 'pending')}"
            for p in graded_picks if p.get("result") not in ("win", "loss", "push")
        ]
        pending_list.extend(pending_examples[:10])

    record = {
        "date": card_date,
        "created_at": _now_iso(),
        "snapshot_created_at": snapshot.get("created_at"),
        "model_version": snapshot.get("model_version"),
        "overall_record": {
            "wins": total_wins,
            "losses": total_losses,
            "pushes": total_pushes,
            "total": total,
            "units": round(total_units, 2),
            "roi_pct": roi,
        },
        "market_records": results_summary,
        "pending": pending_list[:50],
        "manual_review": manual_review,
        "snapshot_path": str(_results_path(card_date).parent / f"{card_date}.json"),
    }
    return record


def _run_ai_learning(card_date: str) -> None:
    """Run AI learning review after grading."""
    try:
        from ai_learning_engine import run_learning_review
        run_learning_review(card_date)
        _log_vault("ai_learning_completed", card_date, "")
    except Exception:
        logger.exception("AI learning failed for %s", card_date)
        _log_vault("ai_learning_failed", card_date, "unexpected error")


# ── Load results ──────────────────────────────────────────────────────────

def load_results(card_date: str) -> dict[str, Any]:
    path = _results_path(card_date)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        logger.exception("Failed to load results for %s", card_date)
        return {}


def vault_status(card_date: str) -> dict[str, Any]:
    path = _results_path(card_date)
    exists = path.exists()
    payload = load_results(card_date) if exists else {}
    snap = snapshot_status(card_date)
    return {
        "snapshot_exists": snap.get("exists", False),
        "results_exist": exists,
        "results_path": str(path) if exists else None,
        "overall_record": payload.get("overall_record") if exists else None,
        "market_records": list((payload.get("market_records") or {}).keys()) if exists else [],
        "pending": len(_list([payload]) if exists else []),
        "errors": [],
    }


def vault_debug(card_date: str) -> dict[str, Any]:
    snap = load_snapshot(card_date)
    results = load_results(card_date)
    return {
        "snapshot_loaded": bool(snap),
        "snapshot_picks_count": len(_list(snap.get("official_picks"))) if snap else 0,
        "results_exist": bool(results),
        "games_graded": len(results.get("market_records", {})) if results else 0,
        "picks_graded": sum(
            r.get("wins", 0) + r.get("losses", 0) + r.get("pushes", 0)
            for r in (results.get("market_records") or {}).values()
        ) if results else 0,
        "pending": results.get("pending", []) if results else [],
        "manual_review": results.get("manual_review", []) if results else [],
        "errors": [],
    }


# ── Render ────────────────────────────────────────────────────────────────

def render_daily_results(card_date: str) -> str:
    """Render a clean daily results summary from the vault."""
    results = load_results(card_date)
    if not results:
        return "No official snapshot saved for this date."

    overall = results.get("overall_record") or {}
    markets = results.get("market_records") or {}
    display_date = _display_date(card_date)

    lines = [
        "⚾ BETGPTAI DAILY RESULTS",
        f"📅 {display_date}",
        "",
        f"Overall:",
        f"W-L-P: {overall.get('wins', 0)}-{overall.get('losses', 0)}-{overall.get('pushes', 0)}",
        f"Units: {overall.get('units', 0):+.2f}",
        f"ROI: {overall.get('roi_pct', 0):+.1f}%",
        "",
        "By Market:",
    ]

    for market in ("play_of_day", "moneyline", "runline", "f5", "game_total", "team_total", "parlay", "prop"):
        rec = markets.get(market)
        if not rec:
            continue
        w, l, p = rec.get("wins", 0), rec.get("losses", 0), rec.get("pushes", 0)
        label = market.replace("_", " ").title()
        if w + l + p > 0:
            lines.append(f"{label}: {w}-{l}-{p} ({rec.get('units', 0):+.2f}u)")
        elif rec.get("pending", 0) > 0:
            lines.append(f"{label}: {rec.get('pending')} pending")

    pending = results.get("pending") or []
    if pending:
        lines.extend(["", "Pending/Manual Review:"])
        lines.extend(f"- {item}" for item in pending[:10])

    return "\n".join(lines).strip()


def render_vault_debug(payload: dict[str, Any]) -> str:
    lines = [
        "📸 BETGPTAI VAULT DEBUG",
        f"Snapshot loaded: {'✅' if payload.get('snapshot_loaded') else '❌'}",
        f"Snapshot picks: {payload.get('snapshot_picks_count', 0)}",
        f"Results exist: {'✅' if payload.get('results_exist') else '❌'}",
        f"Games graded: {payload.get('games_graded', 0)}",
        f"Picks graded: {payload.get('picks_graded', 0)}",
    ]
    pending = payload.get("pending") or []
    if pending:
        lines.append(f"Pending ({len(pending)}):")
        lines.extend(f"- {item}" for item in pending[:10])
    manual = payload.get("manual_review") or []
    if manual:
        lines.append(f"Manual review ({len(manual)}):")
        lines.extend(f"- {item}" for item in manual[:10])
    errors = payload.get("errors") or []
    if errors:
        lines.append(f"Errors ({len(errors)}):")
        lines.extend(f"- {item}" for item in errors[:5])
    return "\n".join(lines).strip()


def _display_date(card_date: str) -> str:
    try:
        return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")
    except Exception:
        return card_date
