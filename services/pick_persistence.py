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
    if not had_pre_guard_picks and _stats_only_card_mode() and source in _PUBLIC_SOURCES and analysis:
        # Both structured path and legacy parser returned 0 picks.
        # Build picks from the analysis text sections directly (stats-only fallback).
        _stats_section_log = _build_picks_from_sections(analysis, slate, card_date, source, picks)
        picks = _stats_section_log["picks"]
        had_pre_guard_picks = bool(picks)
        # Attach debug log to card so save_official_card can surface it
        if isinstance(card, dict):
            card["_stats_section_debug"] = _stats_section_log
    if _public_market_guard_enabled(source):
        picks = _filter_public_market_context(picks, slate)
    elif _stats_only_card_mode() and source in _PUBLIC_SOURCES:
        # Stats-only card mode: keep picks, mark with stats-only metadata
        stats_filtered: list[dict[str, Any]] = []
        for pick in picks:
            mt = str(pick.get("market_type") or pick.get("market") or "").lower()
            line = pick.get("market_line") or pick.get("line")
            # Block totals/team-totals unless projected line is known
            if mt in ("total", "game_total", "totals", "team_total", "team_totals", "tt") and not line:
                _log_storage({
                    "component": "pick_persistence",
                    "event": "stats_only_totals_skipped_no_line",
                    "card_date": card_date,
                    "pick_id": pick.get("pick_id"),
                    "market": mt,
                    "reason": "No projected line for totals — cannot save as official.",
                })
                continue
            # Mark as stats-only
            pick["odds_status"] = "unavailable"
            pick["market_context_status"] = "stats_only"
            pick["sportsbook"] = "none"
            pick["odds"] = None
            pick["posted_line"] = None
            pick["market_line"] = line
            stats_filtered.append(pick)
        picks = stats_filtered
    if not picks:
        # Build odds-unavailable context from slate
        odds_provider = "unknown"
        odds_events = 0
        matched_games = 0
        if slate:
            odds_events = sum(1 for g in slate if g.get("odds_status") == "available")
            matched_games = odds_events
            odds_provider = str(slate[0].get("market_context", {}).get("provider", "unknown")) if slate else "unknown"
        odds_context = f"odds_provider={odds_provider} odds_events_returned={matched_games} matched_games={matched_games} schedule_games={len(slate)}"
        if had_pre_guard_picks:
            mode_hint = ""
            if _stats_only_card_mode():
                mode_hint = "STATS_ONLY_CARD_MODE is enabled but all picks were filtered (totals without projected line, etc). "
            raise ValueError(
                f"Odds unavailable: providers returned {len(slate) - matched_games} MLB events with no market context for schedule with {len(slate)} games. "
                f"{mode_hint}"
                f"({odds_context}) "
                "Set ADMIN_MARKET_OVERRIDE=true to force save."
            )
        # Only raise generic no-picks error if StructuredCard actually had picks
        card_picks = card.get("official_picks") if isinstance(card, dict) and isinstance(card.get("official_picks"), list) else []
        if card_picks:
            raise ValueError(
                f"No trackable official picks could be extracted from the generated card with {len(card_picks)} StructuredCard picks. "
                f"({odds_context})"
            )
        raise ValueError(
            f"No official picks: StructuredCard official_picks is empty. "
            f"AI analysis may have failed or odds were unavailable. "
            f"({odds_context})"
        )
    normalized = [_normalize_for_contract(pick) for pick in picks]
    return card_date, normalized


def _stats_only_card_mode() -> bool:
    """Allow official picks using stats/model edges when odds are unavailable."""
    return os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


_STATS_SECTION_HEADINGS: list[tuple[str, str]] = [
    ("moneyline", "🔥 PLAY OF THE DAY"),
    ("moneyline", "🏆 TOP 2 MONEYLINE"),
    ("moneyline", "🏆 TOP 5 MONEYLINE"),
    ("f5_moneyline", "🔥 TOP 2 F5 MONEYLINE"),
    ("f5_moneyline", "🔥 TOP 5 F5"),
    ("f5_moneyline", "🔥 F5 MONEYLINE LEAN"),
    ("runline", "📈 TOP 2 RUNLINE/SPREAD"),
    ("runline", "📈 TOP 5 RUNLINE/SPREAD"),
    ("runline", "📈 TOP 5 RUN LINE"),
    ("total", "🎯 TOP 2 OVER/UNDER TOTAL RUNS"),
    ("total", "🎯 TOP 5 OVER/UNDER TOTAL RUNS"),
    ("total", "🎯 TOP 5 GAME TOTALS"),
    ("team_total", "💰 TOP 2 TEAM TOTALS"),
    ("team_total", "💰 TEAM TOTAL ANGLE"),
    ("team_total", "💰 TOP 5 TEAM TOTALS"),
    ("parlay", "🧩 2-LEG SAFE PARLAY"),
    ("parlay", "🧩 SAFE PARLAY OF THE DAY"),
]


def _normalize_team_text(name: str) -> str:
    """Lowercase and strip non-alphanumeric for fuzzy team matching."""
    import re
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _team_key(selection: str) -> str:
    """Normalized search key from a pick line (e.g. 'New York Yankees -150' → 'newyorkyankees150')."""
    return _normalize_team_text(selection)


def _match_game(selection: str, slate: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a pick line to a slate game by normalized team name."""
    skey = _team_key(selection)
    matches: list[dict[str, Any]] = []
    for game in slate:
        for k in ("away_team", "home_team"):
            full = _normalize_team_text(str(game.get(k, "")))
            nick = _normalize_team_text(str(game.get(k, "")).split()[-1]) if game.get(k) else ""
            if (full and full in skey) or (len(nick) >= 3 and nick in skey):
                matches.append(game)
                break
    if len(matches) == 1:
        return matches[0]
    unique = {g.get("game_pk") or g.get("game_id"): g for g in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _extract_team_from_selection(selection: str, game: dict[str, Any]) -> str | None:
    """Return which side (away/home) the selection refers to, if any."""
    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))
    norm_away = _normalize_team_text(away)
    norm_home = _normalize_team_text(home)
    norm_sel = _normalize_team_text(selection)
    if norm_away and norm_away in norm_sel:
        return away
    if norm_home and norm_home in norm_sel:
        return home
    return None


def _stats_market_type(category: str) -> str:
    return {
        "moneyline": "moneyline",
        "f5_moneyline": "f5_moneyline",
        "runline": "runline",
        "total": "total",
        "team_total": "team_total",
        "parlay": "parlay",
    }.get(category, "moneyline")


def _parse_line_value(selection: str, market_type: str) -> float | None:
    """Extract a numeric line from the pick text (totals, team-totals, runlines)."""
    import re
    if market_type in ("total", "team_total"):
        m = re.search(r"(?:Over|Under)\s+(\d+(?:\.\d+)?)", selection, flags=re.I)
        return float(m.group(1)) if m else None
    if market_type == "runline":
        m = re.search(r"[+-]\d+(?:\.\d+)?", selection)
        return float(m.group()) if m else None
    return None


def _extract_section_content(analysis: str, heading: str) -> str:
    """Return content between *heading* and the next divider or section heading."""
    start = analysis.find(heading)
    if start < 0:
        return ""
    body_start = start + len(heading)
    # Find next section heading or divider
    rest = analysis[body_start:]
    end = len(analysis)
    for marker in ("\n---", "\n━━━", "\n🔥", "\n🏆", "\n📈", "\n🎯", "\n💰", "\n🧩"):
        pos = analysis.find(marker, body_start + 1)
        if pos >= 0:
            end = min(end, pos)
    return analysis[body_start:end].strip() if end > body_start else rest.strip()


def _parse_pick_lines_from_section(content: str) -> list[str]:
    """Extract individual pick lines from a section body."""
    lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip leading emoji/number bullets
        cleaned = re.sub(r"^[⚾1️⃣2️⃣3️⃣4️⃣5️⃣✅*_`\d.)\s]+", "", stripped).strip()
        if not cleaned:
            continue
        if cleaned.startswith(("Risk", "Line", "Safer", "No", "None", "Unavailable", "🆚")):
            continue
        if len(cleaned) < 5:
            continue
        lines.append(cleaned)
    return lines


def _build_picks_from_sections(
    analysis: str,
    slate: list[dict[str, Any]],
    card_date: str,
    source: str,
    existing_picks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Parse analysis text sections into stats-only pick dicts.

    Only called when the structured path and legacy parser both returned 0 picks
    and STATS_ONLY_CARD_MODE is enabled.  Returns a debug log dict with keys:
      picks, sections_found, section_item_counts, rejected_items, rejection_reasons.
    """
    import hashlib
    import re as _re
    from datetime import datetime, timezone

    log: dict[str, Any] = {
        "picks": list(existing_picks),
        "sections_found": [],
        "section_item_counts": {},
        "rejected_items": [],
        "rejection_reasons": [],
    }

    for category, heading in _STATS_SECTION_HEADINGS:
        if heading not in analysis:
            continue
        content = _extract_section_content(analysis, heading)
        if not content:
            continue
        log["sections_found"].append(heading)

        if category == "parlay":
            # Parlay legs: find ✅ lines
            import re as _re2
            leg_lines = _re2.findall(r"(?m)^✅\s+(.+)$", content)[:2]
            log["section_item_counts"][heading] = len(leg_lines)
            if len(leg_lines) == 2:
                for leg in leg_lines:
                    game = _match_game(leg, slate)
                    if not game:
                        log["rejected_items"].append(leg)
                        log["rejection_reasons"].append(f"Parlay leg no game match: {leg[:60]}")
                        continue
                    pick = _build_single_stats_pick(leg, "moneyline", game, card_date)
                    if pick:
                        log["picks"].append(pick)
                    else:
                        log["rejected_items"].append(leg)
                        log["rejection_reasons"].append(f"Parlay leg build failed: {leg[:60]}")
            continue

        pick_lines = _parse_pick_lines_from_section(content)
        log["section_item_counts"][heading] = len(pick_lines)

        for line in pick_lines:
            market_type = _stats_market_type(category)
            game = _match_game(line, slate)

            if not game:
                log["rejected_items"].append(line)
                log["rejection_reasons"].append(f"No game match for market={market_type}: {line[:60]}")
                continue

            # For totals/team-totals, require a line value
            line_value = _parse_line_value(line, market_type)
            if market_type in ("total", "team_total") and line_value is None:
                log["rejected_items"].append(line)
                log["rejection_reasons"].append(f"No line value for {market_type}: {line[:60]}")
                continue

            pick = _build_single_stats_pick(line, market_type, game, card_date, line_value=line_value)
            if pick:
                log["picks"].append(pick)
            else:
                log["rejected_items"].append(line)
                log["rejection_reasons"].append(f"Pick build failed for market={market_type}: {line[:60]}")

    # Write section-build debug log
    _log_storage({
        "component": "pick_persistence",
        "event": "stats_sections_build",
        "card_date": card_date,
        "source": source,
        "sections_found_count": len(log["sections_found"]),
        "section_item_counts": log["section_item_counts"],
        "total_converted": len(log["picks"]) - len(existing_picks),
        "total_rejected": len(log["rejected_items"]),
    })
    return log


def _build_single_stats_pick(
    selection: str,
    market_type: str,
    game: dict[str, Any],
    card_date: str,
    line_value: float | None = None,
) -> dict[str, Any] | None:
    """Build a single pick dict from a selection line (stats-only, no odds required)."""
    import hashlib

    selected_team: str | None = None
    opponent: str | None = None
    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))

    if market_type not in ("total", "parlay"):
        selected_team = _extract_team_from_selection(selection, game)
        if not selected_team:
            # Fallback: try the longer team name match
            norm_sel = _normalize_team_text(selection)
            for t in (away, home):
                if _normalize_team_text(t) and _normalize_team_text(t) in norm_sel:
                    selected_team = t
                    break
        if selected_team:
            opponent = home if _normalize_team_text(selected_team) == _normalize_team_text(away) else away

    game_pk = game.get("game_pk") or game.get("game_id")
    if isinstance(game_pk, list):
        game_pk = game_pk[0] if game_pk else None

    quant = game.get("betgptai_quant_v20") or game.get("betgptai_internal") or {}
    if isinstance(quant, dict) and isinstance(quant.get("v20"), dict):
        quant = quant["v20"]

    raw_id = "|".join(str(p or "") for p in [card_date, str(game_pk), market_type, selected_team or "", str(line_value or "")])
    pick_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]

    return {
        "pick_id": pick_id,
        "sport": "mlb",
        "league": "MLB",
        "card_date": card_date,
        "game_pk": int(game_pk) if game_pk is not None else None,
        "away_team": away,
        "home_team": home,
        "selected_team": selected_team,
        "opponent": opponent,
        "market": market_type,
        "market_type": market_type,
        "market_line": line_value,
        "odds": None,
        "confidence": quant.get("confidence"),
        "edge_score": quant.get("final_edge_score") or quant.get("edge_score"),
        "risk": quant.get("risk_level") or quant.get("risk"),
        "units": 1.0,
        "reason": "",
        "status": "pending",
        "result": None,
        "model_version": "BETGPTAI v20.0",
        "odds_status": "unavailable",
        "market_context_status": "stats_only",
        "sportsbook": "none",
        "posted_line": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


_PUBLIC_SOURCES = frozenset({
    "mlb_auto",
    "generate_today",
    "force_generate_today",
    "scheduled_generate",
    "scheduled_t45_generation",
    "scheduled_post",
    "today",
    "card_debug",
    "save_today_picks",
})


def _public_market_guard_enabled(source: str) -> bool:
    """Public cards need market context unless owner explicitly overrides."""
    if os.getenv("ADMIN_MARKET_OVERRIDE", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    if _stats_only_card_mode():
        return False
    return source in _PUBLIC_SOURCES


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
    """Save an official generated card to picks.json, then trigger daily snapshot.

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
            # Trigger immutable daily snapshot after successful save
            try:
                from services.daily_snapshot import save_daily_snapshot

                slate = []
                if isinstance(card, dict):
                    raw_slate = card.get("slate")
                    if isinstance(raw_slate, list):
                        slate = raw_slate
                source = str(card.get("source_command", "structured_card")) if isinstance(card, dict) else "structured_card"
                snapshot_result = save_daily_snapshot(
                    card_date=card_date,
                    picks=existing + saved,
                    slate=slate,
                    source=source,
                )
                if snapshot_result.get("success"):
                    _log_storage({
                        "component": "pick_persistence",
                        "event": "daily_snapshot_created",
                        "card_date": card_date,
                        "snapshot_path": snapshot_result.get("path"),
                    })
                elif snapshot_result.get("reason") == "already_exists":
                    _log_storage({
                        "component": "pick_persistence",
                        "event": "daily_snapshot_skipped_exists",
                        "card_date": card_date,
                    })
                else:
                    _log_storage({
                        "component": "pick_persistence",
                        "event": "daily_snapshot_failed",
                        "card_date": card_date,
                        "error": snapshot_result.get("error"),
                    })
            except Exception as snap_error:
                _log_storage({
                    "component": "pick_persistence",
                    "event": "daily_snapshot_error",
                    "card_date": card_date,
                    "error": repr(snap_error),
                })
            sdb = card.get("_stats_section_debug") if isinstance(card, dict) else None
            status = {
                "success": True,
                "error": "",
                "saved_pick_count": len(saved),
                "card_date": card_date,
                "path": str(PICKS_FILE),
                "last_save_time": _now_iso(),
                "stats_section_debug": sdb,
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
