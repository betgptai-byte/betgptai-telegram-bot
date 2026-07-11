"""BETGPTAI Daily Snapshot — immutable official card record for results grading.

Saves the exact posted picks, lines, odds, edge scores, and market context
immediately after StructuredCard official_picks are persisted.  Never
overwrites an existing snapshot; only the owner can force-regenerate via
``/snapshot_regenerate YYYY-MM-DD --confirm``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import DATA_DIR, data_file

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
HISTORY_ROOT = DATA_DIR / "history"
SNAPSHOT_LOG = DATA_DIR / "logs" / "snapshot.log"
SNAPSHOT_LOG.parent.mkdir(parents=True, exist_ok=True)


# ── Helpers ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=10,
        ).decode("utf-8").strip()
    except Exception:
        return "unknown"


def _log_snapshot(event: str, card_date: str, details: str = "") -> None:
    payload = {
        "timestamp": _now_iso(),
        "event": event,
        "card_date": card_date,
        "details": details,
    }
    with SNAPSHOT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _snapshot_path(card_date: str) -> Path:
    parts = card_date.split("-")
    if len(parts) == 3:
        year, month = parts[0], parts[1]
    else:
        year, month = card_date[:4], card_date[5:7]
    return HISTORY_ROOT / year / month / f"{card_date}.json"


def _snapshot_exists(card_date: str) -> bool:
    return _snapshot_path(card_date).exists()


# ── Build snapshot ─────────────────────────────────────────────────────────

def _weather_snapshot(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots = []
    for game in slate:
        weather = _dict(game.get("weather"))
        if weather:
            snapshots.append({
                "game_pk": game.get("game_pk") or game.get("game_id"),
                "game": f"{game.get('away_team')} @ {game.get('home_team')}",
                "summary": weather.get("summary"),
                "wind": weather.get("wind"),
                "temp": weather.get("temp") or weather.get("temperature"),
                "humidity": weather.get("humidity"),
                "roof": weather.get("roof"),
            })
    return snapshots


def _lineup_snapshot(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots = []
    for game in slate:
        snapshots.append({
            "game_pk": game.get("game_pk") or game.get("game_id"),
            "game": f"{game.get('away_team')} @ {game.get('home_team')}",
            "lineup_status": "confirmed" if game.get("lineups") not in (None, "", "unavailable", [], {}) else "projected",
            "away_lineup": game.get("away_lineup") or game.get("lineups", {}).get("away"),
            "home_lineup": game.get("home_lineup") or game.get("lineups", {}).get("home"),
        })
    return snapshots


def _starting_pitchers_snapshot(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots = []
    for game in slate:
        sp = _dict(game.get("starting_pitchers"))
        snapshots.append({
            "game_pk": game.get("game_pk") or game.get("game_id"),
            "game": f"{game.get('away_team')} @ {game.get('home_team')}",
            "away_pitcher": _dict(sp.get("away")).get("name"),
            "home_pitcher": _dict(sp.get("home")).get("name"),
        })
    return snapshots


def _edge_scores_from_picks(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    edges = []
    for pick in picks:
        edge = pick.get("edge_score") or pick.get("final_edge_score") or pick.get("quant_edge_score")
        edges.append({
            "pick_id": pick.get("pick_id"),
            "market_type": pick.get("market_type") or pick.get("market"),
            "selection": pick.get("selected_team") or pick.get("pick_text"),
            "edge_score": edge,
            "confidence": pick.get("confidence") or pick.get("confidence_grade"),
            "risk": pick.get("risk") or pick.get("risk_level"),
            "units": pick.get("units") or pick.get("units_risked", 1),
            "opening_line": pick.get("opening_line"),
            "posted_line": pick.get("market_line") or pick.get("line"),
            "closing_line": pick.get("closing_line"),
            "clv": pick.get("clv"),
            "odds_provider": pick.get("odds_provider"),
            "odds_timestamp": pick.get("odds_timestamp"),
        })
    return edges


def _market_context_snapshot(slate: list[dict[str, Any]]) -> dict[str, Any]:
    import os
    matched = sum(1 for g in slate if g.get("odds_status") == "available")
    total = len(slate)
    stats_only = os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "games_with_odds": matched,
        "total_games": total,
        "provider": "Sharp API / Odds API" if matched > 0 else "None",
        "matched_ratio": f"{matched}/{total}",
        "market_mode": "stats_only" if stats_only else "normal",
    }


def _build_snapshot(
    card_date: str,
    picks: list[dict[str, Any]],
    slate: list[dict[str, Any]],
    source: str = "structured_card",
    model_version: str = "BETGPTAI v20.0",
) -> dict[str, Any]:
    """Build an immutable snapshot dict from official picks and slate context."""

    regular = [p for p in picks if not p.get("admin_market_fallback") and not p.get("inferred_line_admin_only")]
    admin_only = [p for p in picks if p.get("admin_market_fallback") or p.get("inferred_line_admin_only")]

    play_of_day = [p for p in regular if str(p.get("market_type") or p.get("market") or "").lower() in {"play_of_day", "play_of_the_day"} or (p.get("pick_text") or "").startswith("🔥")]
    moneylines = [p for p in regular if (p.get("market_type") or p.get("market") or "").lower() in {"moneyline", "h2h", "ml"}]
    runlines = [p for p in regular if (p.get("market_type") or p.get("market") or "").lower() in {"runline", "spreads", "rl"}]
    f5_picks = [p for p in regular if (p.get("market_type") or p.get("market") or "").lower() in {"f5_moneyline", "f5"}]
    game_totals = [p for p in regular if (p.get("market_type") or p.get("market") or "").lower() in {"total", "game_total", "totals"}]
    team_totals = [p for p in regular if (p.get("market_type") or p.get("market") or "").lower() in {"team_total", "team_totals", "tt"}]
    safe_parlay = [p for p in regular if p.get("parlay_leg") or str(p.get("pick_text") or "").startswith("SAFE") or str(p.get("market_type") or "").lower() == "parlay"]
    core_five = [p for p in regular if str(p.get("category") or "").lower() == "core_five"]
    props = [p for p in regular if str(p.get("market_type") or p.get("market") or "").lower() in {"player_prop", "prop", "hitter_prop", "pitcher_prop"}]

    try:
        import model_weights as mw
        weights = mw.load_model_weights()
    except Exception:
        weights = {}

    market_ctx = _market_context_snapshot(slate)
    snapshot = {
        "date": card_date,
        "created_at": _now_iso(),
        "posted_at": None,
        "model_version": model_version,
        "git_commit": _git_commit(),
        "source": source,
        "market_provider": market_ctx.get("provider", "None"),
        "market_mode": market_ctx.get("market_mode", "normal"),
        "official_picks_count": len(regular),
        "admin_only_picks_count": len(admin_only),
        "official_picks": regular,
        "admin_only_picks": admin_only,
        "play_of_day": play_of_day,
        "moneylines": moneylines,
        "runlines": runlines,
        "f5": f5_picks,
        "game_totals": game_totals,
        "team_totals": team_totals,
        "safe_parlay": safe_parlay,
        "core_five": core_five,
        "props": props,
        "edge_scores": _edge_scores_from_picks(regular),
        "weather_snapshot": _weather_snapshot(slate),
        "lineup_snapshot": _lineup_snapshot(slate),
        "starting_pitchers_snapshot": _starting_pitchers_snapshot(slate),
        "market_context_snapshot": _market_context_snapshot(slate),
        "line_snapshot": {
            "opening_lines": {},  # filled by odds_provider_router when available
            "posted_lines": {p.get("pick_id"): p.get("market_line") or p.get("line") for p in regular},
            "closing_lines": {},  # filled post-game by results_vault
            "clv_records": {},    # filled post-game by results_vault
        },
        "weights_snapshot": weights,
    }
    return snapshot


def save_daily_snapshot(
    card_date: str,
    picks: list[dict[str, Any]],
    slate: list[dict[str, Any]],
    source: str = "structured_card",
    model_version: str = "BETGPTAI v20.0",
) -> dict[str, Any]:
    """Save an immutable daily snapshot.

    Never overwrites an existing snapshot.  Returns ``{"success": True, "path": str}``
    or ``{"success": False, "error": str, "reason": "already_exists"}``.
    """
    if not card_date:
        return {"success": False, "error": "card_date is required", "reason": "invalid_date"}
    path = _snapshot_path(card_date)
    if path.exists():
        _log_snapshot("snapshot_skipped_exists", card_date, str(path))
        return {"success": False, "error": f"Snapshot already exists at {path}", "reason": "already_exists"}

    try:
        snapshot = _build_snapshot(card_date, picks, slate, source, model_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)
        _log_snapshot("snapshot_created", card_date, str(path))
        return {"success": True, "path": str(path), "picks_count": len(picks)}
    except Exception as error:
        logger.exception("Failed to save daily snapshot for %s", card_date)
        _log_snapshot("snapshot_failed", card_date, repr(error))
        return {"success": False, "error": repr(error), "reason": "write_error"}


def regenerate_snapshot(
    card_date: str,
    picks: list[dict[str, Any]],
    slate: list[dict[str, Any]],
    source: str = "structured_card",
) -> dict[str, Any]:
    """Force-regenerate a snapshot (owner-confirmed only)."""
    path = _snapshot_path(card_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        snapshot = _build_snapshot(card_date, picks, slate, source)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)
        _log_snapshot("snapshot_regenerated", card_date, str(path))
        return {"success": True, "path": str(path), "picks_count": len(picks)}
    except Exception as error:
        logger.exception("Failed to regenerate snapshot for %s", card_date)
        return {"success": False, "error": repr(error)}


def load_snapshot(card_date: str) -> dict[str, Any]:
    """Load an existing snapshot, or return empty dict."""
    path = _snapshot_path(card_date)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        logger.exception("Failed to load snapshot for %s", card_date)
        return {}


def snapshot_status(card_date: str) -> dict[str, Any]:
    """Return status info for a given date."""
    path = _snapshot_path(card_date)
    exists = path.exists()
    payload = load_snapshot(card_date) if exists else {}
    return {
        "exists": exists,
        "path": str(path) if exists else None,
        "official_picks_count": len(_list(payload.get("official_picks"))) if exists else 0,
        "markets_saved": {
            "play_of_day": bool(payload.get("play_of_day")),
            "moneylines": len(_list(payload.get("moneylines"))),
            "runlines": len(_list(payload.get("runlines"))),
            "f5": len(_list(payload.get("f5"))),
            "game_totals": len(_list(payload.get("game_totals"))),
            "team_totals": len(_list(payload.get("team_totals"))),
            "safe_parlay": bool(payload.get("safe_parlay")),
            "core_five": len(_list(payload.get("core_five"))),
            "props": len(_list(payload.get("props"))),
        } if exists else {},
        "created_at": payload.get("created_at") if exists else None,
        "locked": exists,
        "market_mode": payload.get("market_mode", "normal"),
    }


def snapshot_debug(card_date: str) -> dict[str, Any]:
    """Return debug info for owner diagnostics."""
    path = _snapshot_path(card_date)
    exists = path.exists()
    payload = load_snapshot(card_date) if exists else {}
    official = _list(payload.get("official_picks"))
    admin = _list(payload.get("admin_only_picks"))
    return {
        "exists": exists,
        "path": str(path) if exists else None,
        "official_picks": official,
        "admin_only_picks": admin,
        "admin_only_count": len(admin),
        "market_mode": payload.get("market_mode", "normal"),
        "missing_fields": [k for k in [
            "date", "created_at", "model_version", "git_commit", "source",
            "official_picks", "moneylines", "edge_scores", "weather_snapshot",
        ] if k not in payload] if exists else [],
    }


def render_snapshot_status(payload: dict[str, Any]) -> str:
    if not payload.get("exists"):
        return "No official snapshot saved for this date."
    markets = payload.get("markets_saved") or {}
    mm = payload.get("market_mode", "normal")
    mode_line = f"\nMarket Mode: {'Stats Only' if mm == 'stats_only' else 'Normal'}"
    return (
        "📸 BETGPTAI DAILY SNAPSHOT STATUS\n\n"
        f"Snapshot exists: ✅\n"
        f"Path: {payload.get('path')}\n"
        f"Official picks: {payload.get('official_picks_count')}\n"
        f"Created at: {payload.get('created_at')}\n"
        f"Locked: {'✅' if payload.get('locked') else '❌'}"
        f"{mode_line}\n\n"
        "Markets saved:\n"
        f"- Play of Day: {'✅' if markets.get('play_of_day') else '❌'}\n"
        f"- Moneylines: {markets.get('moneylines', 0)}\n"
        f"- Runlines: {markets.get('runlines', 0)}\n"
        f"- F5: {markets.get('f5', 0)}\n"
        f"- Game Totals: {markets.get('game_totals', 0)}\n"
        f"- Team Totals: {markets.get('team_totals', 0)}\n"
        f"- Safe Parlay: {'✅' if markets.get('safe_parlay') else '❌'}\n"
        f"- Core Five: {markets.get('core_five', 0)}\n"
        f"- Props: {markets.get('props', 0)}"
    )


def render_clv_debug(payload: dict[str, Any]) -> str:
    """Render CLV debug for a loaded snapshot."""
    lines = [
        "📊 BETGPTAI CLV DEBUG",
        f"📅 Date: {payload.get('card_date', '?')}",
    ]
    scores = _list(payload.get("edge_scores", payload.get("official_picks", [])))
    clv_picks = [p for p in scores if p.get("clv") is not None]
    no_clv = [p for p in scores if p.get("clv") is None]
    if not clv_picks:
        lines.append("No CLV data found.")
        if no_clv:
            lines.append(f"({len(no_clv)} picks without CLV)")
        return "\n".join(lines).strip()
    positive = sum(1 for p in clv_picks if float(p.get("clv", 0)) > 0)
    negative = sum(1 for p in clv_picks if float(p.get("clv", 0)) <= 0)
    lines.extend([
        f"Picks with CLV: {len(clv_picks)}",
        f"Positive CLV: {positive} / Negative CLV: {negative}",
    ])
    for p in clv_picks[:20]:
        clv = float(p.get("clv", 0))
        lines.append(
            f"  {p.get('pick_text') or p.get('selected_team', '?')} — "
            f"CLV {clv:+.3f} — open {p.get('opening_line', '?')} "
            f"→ posted {p.get('posted_line', '?')} → close {p.get('closing_line', '?')} — "
            f"result {p.get('result', 'pending')}"
        )
    if no_clv:
        lines.append(f"No CLV ({len(no_clv)}):")
        for p in no_clv[:5]:
            lines.append(f"  {p.get('pick_text') or p.get('selected_team', '?')}")
    return "\n".join(lines).strip()


def render_snapshot_debug(payload: dict[str, Any]) -> str:
    if not payload.get("exists"):
        return "No official snapshot saved for this date."
    admin = payload.get("admin_only_picks") or []
    missing = payload.get("missing_fields") or []
    lines = [
        "📸 BETGPTAI SNAPSHOT DEBUG",
        f"Path: {payload.get('path')}",
        f"Official picks: {len(payload.get('official_picks') or [])}",
        f"Admin-only picks excluded: {payload.get('admin_only_count', 0)}",
        f"Market Mode: {payload.get('market_mode', 'normal')}",
    ]
    if admin:
        lines.append("Admin-only excluded picks:")
        for p in admin[:10]:
            lines.append(f"  - {p.get('pick_text') or p.get('selected_team')}")
    if missing:
        lines.append(f"Missing fields: {', '.join(missing)}")
    else:
        lines.append("All required fields present.")
    return "\n".join(lines).strip()
