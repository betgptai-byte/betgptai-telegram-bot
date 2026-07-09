"""Dedicated BETGPTAI Pick Persistence Service.

This is the single service responsible for writing official picks to
``picks.json``. It repairs missing/corrupt storage, writes atomically through
``picks.tmp``, retries once on failure, and records every save attempt to
``logs/storage.log``.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from storage import DATA_DIR, data_file, ensure_json_file


EASTERN = ZoneInfo("America/New_York")
PICKS_FILE = data_file("picks.json")
RESULTS_FILE = data_file("results.json")
POSTING_LOG_FILE = data_file("posting_log.json")
STORAGE_LOG_FILE = data_file("logs") / "storage.log"
STATUS_FILE = data_file("pick_persistence_status.json")


def _now_iso() -> str:
    return datetime.now(EASTERN).isoformat(timespec="seconds")


def _timestamp() -> str:
    return datetime.now(EASTERN).strftime("%Y%m%d_%H%M%S")


def _log_storage(event: dict[str, Any]) -> None:
    """Append one JSON line to logs/storage.log."""
    STORAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": _now_iso(), **event}
    with STORAGE_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")


def _write_status(payload: dict[str, Any]) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _read_status() -> dict[str, Any]:
    try:
        payload = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _backup_corrupt_picks() -> Path | None:
    if not PICKS_FILE.exists():
        return None
    backup = data_file(f"picks_corrupt_{_timestamp()}.json")
    shutil.move(str(PICKS_FILE), str(backup))
    return backup


def _ensure_picks_file() -> list[dict[str, Any]]:
    """Create or repair picks.json and return its list payload."""
    PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PICKS_FILE.exists():
        _atomic_write([])
        return []
    try:
        payload = json.loads(PICKS_FILE.read_text(encoding="utf-8") or "[]")
        if not isinstance(payload, list):
            raise ValueError("picks.json must contain a list")
        return [pick for pick in payload if isinstance(pick, dict)]
    except Exception:
        backup = _backup_corrupt_picks()
        _atomic_write([])
        _log_storage(
            {
                "component": "pick_persistence",
                "event": "picks_corrupt_repaired",
                "card_date": None,
                "pick_count": 0,
                "save_path": str(PICKS_FILE),
                "exception": f"Corrupt picks.json moved to {backup}",
            }
        )
        return []


def _atomic_write(picks: list[dict[str, Any]]) -> None:
    """Write picks atomically through DATA_DIR/picks.tmp then rename."""
    PICKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "picks.tmp"
    tmp.write_text(
        json.dumps(picks, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    tmp.replace(PICKS_FILE)


def write_picks_payload(picks: list[dict[str, Any]], event: str = "write_picks_payload") -> None:
    """Centralized low-level picks.json writer for grading/repair workflows too."""
    if not isinstance(picks, list):
        raise ValueError("picks.json payload must be a list")
    _atomic_write(picks)
    _log_storage(
        {
            "component": "pick_persistence",
            "event": event,
            "card_date": None,
            "pick_count": len(picks),
            "save_path": str(PICKS_FILE),
            "exception": "",
        }
    )


def _dedupe_key(pick: dict[str, Any]) -> tuple[str, str, str, str, str]:
    selected = str(pick.get("selected_team") or pick.get("player_id") or pick.get("player_name") or "")
    return (
        str(pick.get("card_date") or ""),
        str(pick.get("game_pk") or ""),
        str(pick.get("market_type") or pick.get("market") or ""),
        selected,
        str(pick.get("market_line") if pick.get("market_line") is not None else pick.get("line") or ""),
    )


def _validate_required(pick: dict[str, Any]) -> None:
    required = {
        "pick_id",
        "card_date",
        "created_at",
        "game_pk",
        "sport",
        "league",
        "home_team",
        "away_team",
        "selected_team",
        "market",
        "market_type",
        "market_line",
        "odds",
        "confidence",
        "edge_score",
        "risk",
        "units",
        "reason",
        "status",
        "result",
        "model_version",
    }
    missing = [field for field in sorted(required) if field not in pick]
    if missing:
        raise ValueError("Official pick missing required fields: " + ", ".join(missing))


def _normalize_for_contract(pick: dict[str, Any]) -> dict[str, Any]:
    """Normalize existing tracker pick shape into the persistence contract."""
    normalized = dict(pick)
    normalized["market"] = normalized.get("market") or normalized.get("market_type")
    normalized["market_type"] = normalized.get("market_type") or normalized.get("market")
    normalized["market_line"] = normalized.get("market_line", normalized.get("line"))
    normalized["edge_score"] = normalized.get("edge_score", normalized.get("final_edge_score"))
    normalized["risk"] = normalized.get("risk", normalized.get("risk_level"))
    normalized["units"] = normalized.get("units", normalized.get("units_risked", 1))
    normalized["league"] = normalized.get("league") or ("MLB" if normalized.get("sport") == "mlb" else "")
    normalized["status"] = "pending"
    normalized["result"] = None
    normalized.setdefault("created_at", _now_iso())
    normalized.setdefault("odds", None)
    normalized.setdefault("selected_team", None)
    normalized.setdefault("reason", "")
    normalized.setdefault("model_version", "BETGPTAI v20.0")
    _validate_required(normalized)
    return normalized


def _extract_card_inputs(card: Any) -> tuple[str, list[dict[str, Any]], str, str]:
    """Accept either a dict card payload or a raw analysis string."""
    if isinstance(card, dict):
        analysis = str(card.get("analysis") or card.get("raw_text") or card.get("text") or "")
        slate = card.get("slate") if isinstance(card.get("slate"), list) else []
        card_date = str(card.get("card_date") or card.get("date") or "")
        source = str(card.get("source_command") or "unknown")
        return analysis, slate, card_date, source
    return str(card or ""), [], "", "unknown"


def _build_official_picks(card: Any) -> tuple[str, list[dict[str, Any]]]:
    """Use the existing parser/model enrichment, then normalize for storage."""
    # Lazy import avoids making results_tracker the storage writer. It only
    # extracts/normalizes pick data; this service performs the write.
    import results_tracker as rt

    analysis, slate, card_date, source = _extract_card_inputs(card)
    if not card_date:
        raise ValueError("card_date is required for official pick persistence")
    if not analysis:
        raise ValueError("analysis/card text is required for official pick persistence")
    if slate and not any(game.get("betgptai_quant_v20") for game in slate):
        try:
            slate = rt.enrich_slate_with_quant_scores(slate, card_date)
        except Exception:
            # Keep save resilient; missing quant should not corrupt storage.
            pass
    explicit = card.get("official_picks") if isinstance(card, dict) and isinstance(card.get("official_picks"), list) else []
    picks = [dict(pick) for pick in explicit if isinstance(pick, dict)]
    if not picks:
        _log_storage({
            "component": "pick_persistence",
            "event": "LEGACY_PICK_PARSER_USED",
            "card_date": card_date,
            "source": source,
            "message": "StructuredCard official_picks key absent or empty; falling back to extract_official_picks (text parsing)",
        })
        picks = rt.extract_official_picks(analysis, slate, card_date, source)
    picks.extend(rt._approved_prop_records(card_date, source))  # approved admin props, if any
    had_pre_guard_picks = bool(picks)
    if _public_market_guard_enabled(source):
        picks = _filter_public_market_context(picks, slate)
    if not picks:
        if had_pre_guard_picks:
            raise ValueError(
                "No public picks saved because no generated picks had matched market context. "
                "Check /odds_debug or set ADMIN_MARKET_OVERRIDE=true for owner override."
            )
        raise ValueError("No trackable official picks were extracted from the generated card")
    normalized = [_normalize_for_contract(pick) for pick in picks]
    return card_date, normalized


def _public_market_guard_enabled(source: str) -> bool:
    """Public cards need market context unless owner explicitly overrides."""
    if os.getenv("ADMIN_MARKET_OVERRIDE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    return source in {
        "mlb_auto",
        "generate_today",
        "force_generate_today",
        "scheduled_generate",
        "scheduled_t45_generation",
        "scheduled_post",
        "today",
        "card_debug",
        "save_today_picks",
    }


def _game_has_market_context(game_pk: Any, slate: list[dict[str, Any]]) -> bool:
    if isinstance(game_pk, list):
        return all(_game_has_market_context(item, slate) for item in game_pk)
    for game in slate:
        if str(game.get("game_pk") or game.get("game_id")) == str(game_pk):
            return bool(game.get("best_available_prices"))
    return False


def _filter_public_market_context(picks: list[dict[str, Any]], slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only public picks whose game has a matched Odds API market."""
    filtered: list[dict[str, Any]] = []
    skipped = 0
    for pick in picks:
        if _game_has_market_context(pick.get("game_pk") or pick.get("game_id"), slate):
            filtered.append(pick)
        else:
            skipped += 1
    if skipped:
        _log_storage(
            {
                "component": "pick_persistence",
                "event": "public_pick_market_context_skipped",
                "card_date": picks[0].get("card_date") if picks else None,
                "pick_count": skipped,
                "save_path": str(PICKS_FILE),
                "exception": "Skipped public picks without matched market context. Set ADMIN_MARKET_OVERRIDE=true to allow.",
            }
        )
    return filtered


def save_official_card(card: Any) -> dict[str, Any]:
    """Save an official generated card to picks.json.

    Returns:
        {"success": bool, "error": str, "saved_pick_count": int}
    """
    last_error = ""
    card_date = ""
    for attempt in (1, 2):
        try:
            card_date, new_picks = _build_official_picks(card)
            existing = _ensure_picks_file()
            existing_keys = {_dedupe_key(pick) for pick in existing}
            saved: list[dict[str, Any]] = []
            seen: set[tuple[str, str, str, str, str]] = set()
            for pick in new_picks:
                key = _dedupe_key(pick)
                if key in existing_keys or key in seen:
                    continue
                seen.add(key)
                saved.append(pick)
            _atomic_write(existing + saved)
            _log_storage(
                {
                    "component": "pick_persistence",
                    "event": "save_official_card",
                    "card_date": card_date,
                    "pick_count": len(saved),
                    "save_path": str(PICKS_FILE),
                    "exception": "",
                }
            )
            status = {
                "success": True,
                "error": "",
                "saved_pick_count": len(saved),
                "card_date": card_date,
                "path": str(PICKS_FILE),
                "last_save_time": _now_iso(),
            }
            _write_status(status)
            return status
        except Exception as error:
            last_error = repr(error)
            _log_storage(
                {
                    "component": "pick_persistence",
                    "event": "save_failed",
                    "card_date": card_date or None,
                    "pick_count": 0,
                    "save_path": str(PICKS_FILE),
                    "exception": last_error,
                    "attempt": attempt,
                }
            )
            if attempt == 1:
                continue
    status = {
        "success": False,
        "error": last_error,
        "saved_pick_count": 0,
        "card_date": card_date,
        "path": str(PICKS_FILE),
        "last_save_time": "",
    }
    _write_status(status)
    return status


def repair_storage() -> dict[str, Any]:
    """Repair core runtime JSON files needed by posting/results."""
    repaired = {}
    for filename in ("picks.json", "results.json", "posting_log.json"):
        repaired[filename] = ensure_json_file(filename)
    return repaired


def save_debug(card_date: str) -> dict[str, Any]:
    """Return owner-only pick persistence diagnostics."""
    repaired = ensure_json_file("picks.json")
    status = _read_status()
    valid = bool(repaired.get("valid"))
    try:
        picks = json.loads(PICKS_FILE.read_text(encoding="utf-8"))
        if not isinstance(picks, list):
            valid = False
            picks = []
    except Exception:
        valid = False
        picks = []
    todays = [
        pick for pick in picks
        if isinstance(pick, dict) and str(pick.get("card_date") or "") == card_date
    ]
    writable = os.access(DATA_DIR, os.W_OK)
    try:
        permissions = oct(PICKS_FILE.stat().st_mode)[-3:] if PICKS_FILE.exists() else "missing"
    except OSError:
        permissions = "unavailable"
    return {
        "data_dir": str(DATA_DIR),
        "picks_path": str(PICKS_FILE),
        "exists": PICKS_FILE.exists(),
        "writable": writable,
        "json_valid": valid,
        "todays_picks": len(todays),
        "last_save_time": status.get("last_save_time") or "Unavailable",
        "last_error": status.get("error") or "None",
        "disk_permissions": permissions,
    }


def render_save_debug(card_date: str) -> str:
    payload = save_debug(card_date)
    return (
        "💾 BETGPTAI SAVE DEBUG\n\n"
        f"DATA_DIR: {payload.get('data_dir')}\n"
        f"picks.json path: {payload.get('picks_path')}\n"
        f"Exists: {'✅' if payload.get('exists') else '❌'}\n"
        f"Writable: {'✅' if payload.get('writable') else '❌'}\n"
        f"JSON valid: {'✅' if payload.get('json_valid') else '❌'}\n"
        f"Today's picks: {payload.get('todays_picks')}\n"
        f"Last save time: {payload.get('last_save_time')}\n"
        f"Last error: {payload.get('last_error')}\n"
        f"Disk permissions: {payload.get('disk_permissions')}"
    )
