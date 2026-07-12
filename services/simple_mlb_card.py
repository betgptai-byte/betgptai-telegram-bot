"""SIMPLE MLB CARD ENGINE v1 — parallel emergency-stable card path.

This module is intentionally independent of the advanced StructuredCard
pipeline.  It builds official picks directly from the quant-enriched slate
and never:

  * parses Telegram card text,
  * requires ``StructuredCard`` / ``build_card_from_analysis``,
  * requires ``extract_official_picks``,
  * requires ``best_available_prices`` or any sportsbook odds.

When odds are unavailable it runs in stats-only mode and still produces a
graded, saved card so the free channel always has content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import DATA_DIR


SIMPLE_CARD_DIR = DATA_DIR / "simple_cards"
SIMPLE_CARD_DIR.mkdir(parents=True, exist_ok=True)

SOURCE = "simple_mlb_card_v1"
MARKET_MODE = "stats_only"

_MARKET_LABELS = {
    "play_of_day": "🔥 PLAY OF THE DAY",
    "moneyline": "🏆 TOP MONEYLINES",
    "f5_moneyline": "🔥 TOP F5",
    "runline": "📈 TOP RUNLINES",
}


def _stats_mode_active(slate: list[dict[str, Any]]) -> bool:
    """True when no game carries usable (real) market/odds context.

    A truthy *string* in ``best_available_prices`` / ``market_context`` (e.g. a
    serialized placeholder) must NOT count as available odds — only a dict with
    actual price/odds data does.
    """
    if not slate:
        return True
    for game in slate:
        if not isinstance(game, dict):
            continue
        ctx = game.get("market_context")
        if isinstance(ctx, dict) and ctx.get("odds"):
            return False
        prices = game.get("best_available_prices")
        if isinstance(prices, dict) and prices:
            return False
        if game.get("odds_status") == "available":
            # Status alone is not enough; require accompanying real data.
            if (isinstance(prices, (dict, list)) and prices) or (isinstance(ctx, dict) and ctx):
                return False
    return True


def _quant_for(game: dict[str, Any]) -> dict[str, Any]:
    quant = (
        game.get("betgptai_quant_v21")
        or game.get("betgptai_quant_v20")
        or game.get("betgptai_internal")
        or {}
    )
    if isinstance(quant, dict) and isinstance(quant.get("v20"), dict):
        quant = quant["v20"]
    return quant if isinstance(quant, dict) else {}


def _favored_side(game: dict[str, Any]) -> tuple[str, str]:
    """Pick the side the model leans using stats-only signals.

    Uses recent 10-game form first; falls back to home field when form is
    tied or unavailable.  Returns (team, opponent).
    """
    away = str(game.get("away_team", "")).strip()
    home = str(game.get("home_team", "")).strip()
    away_wins = float(game.get("away_last_10_wins") or 0)
    home_wins = float(game.get("home_last_10_wins") or 0)
    if away_wins and home_wins and away_wins != home_wins:
        team, opponent = (away, home) if away_wins > home_wins else (home, away)
    else:
        team, opponent = (home, away)
    return team, opponent


def _stable_id(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _make_pick(
    card_date: str,
    market: str,
    game: dict[str, Any],
    team: str,
    opponent: str,
    quant: dict[str, Any],
    *,
    line: float | None = None,
    parlay_leg: bool = False,
) -> dict[str, Any]:
    game_pk = game.get("game_pk") or game.get("game_id")
    edge = quant.get("final_edge_score")
    confidence = quant.get("confidence")
    return {
        "id": _stable_id(SOURCE, card_date, str(game_pk), market, team, str(line or "")),
        "date": card_date,
        "market": market,
        "team": team,
        "opponent": opponent,
        "game_id": game_pk,
        "pick": f"{team} ({market})",
        "confidence": confidence,
        "edge_score": edge,
        "market_mode": MARKET_MODE,
        "odds_status": "unavailable",
        "sportsbook": "none",
        "posted_line": line,
        "line_verified": False,
        "trackable": True,
        "source": SOURCE,
        "parlay_leg": parlay_leg,
        "game_time": game.get("game_time"),
        "model_version": quant.get("model_version", "BETGPTAI v21.0"),
    }


def build_simple_mlb_card(card_date: str | None = None) -> dict:
    """Build a stats-only MLB card dict directly from the enriched slate."""
    from ai_analysis import upcoming_mlb_slate
    from mlb_data import get_combined_slate
    from quant_engine import score_game

    selected_date = card_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    errors: list[str] = []

    raw_slate: list[dict[str, Any]] = []
    try:
        raw_slate = get_combined_slate(
            os.getenv("ODDS_API_KEY", ""),
            game_date=selected_date,
            highlightly_api_key=os.getenv("HIGHLIGHTLY_API_KEY", ""),
        )
    except Exception as error:  # pragma: no cover - network/IO dependent
        errors.append(f"slate_load_failed: {error!r}")

    slate = upcoming_mlb_slate(raw_slate) if raw_slate else []
    if not slate:
        errors.append("No upcoming MLB games available for card date.")

    stats_mode = _stats_mode_active(slate)

    if slate:
        try:
            from quant_engine import score_game

            enriched: list[dict[str, Any]] = []
            for raw in slate:
                # Guard: only dict games can be enriched; skip/drop anything else.
                if not isinstance(raw, dict):
                    continue
                game = dict(raw)
                try:
                    quant = score_game(game)
                    if isinstance(quant, dict):
                        game["betgptai_quant_v21"] = quant
                        game["betgptai_quant_v20"] = quant
                        game["betgptai_internal"] = quant
                except Exception as game_error:  # pragma: no cover - engine dependent
                    # Best-effort enrichment: one bad game must not break the card.
                    logging.warning("simple_mlb_card quant skip for %s: %s", game.get("game_pk"), game_error)
                enriched.append(game)
            slate = enriched
        except Exception as error:  # pragma: no cover - import/setup failure
            errors.append(f"quant_enrich_failed: {error!r}")

    candidates: list[dict[str, Any]] = []
    for game in slate:
        quant = _quant_for(game)
        team, opponent = _favored_side(game)
        candidates.append({
            "game": game,
            "team": team,
            "opponent": opponent,
            "quant": quant,
            "edge": float(quant.get("final_edge_score") or 0.0),
            "confidence": quant.get("confidence"),
            "risk": quant.get("risk_level"),
        })

    # Rank by edge score (highest first); fall back to insertion order.
    candidates.sort(key=lambda c: c["edge"], reverse=True)

    picks: list[dict[str, Any]] = []

    def _gid(cand: dict[str, Any]) -> Any:
        return cand["game"].get("game_pk") or cand["game"].get("game_id")

    def _take(n: int, market: str, *, require_min_edge: bool = False,
              exclude: set[Any] | None = None, skip: int = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        scanned = 0
        for cand in candidates:
            if len(out) >= n:
                break
            if exclude and _gid(cand) in exclude:
                continue
            if require_min_edge and cand["edge"] <= 0:
                continue
            scanned += 1
            if scanned <= skip:
                continue
            picks.append(_make_pick(selected_date, market, cand["game"], cand["team"], cand["opponent"], cand["quant"]))
            out.append(picks[-1])
        return out

    # Play of the Day: single highest-edge pick.
    play_of_day: list[dict[str, Any]] = _take(1, "play_of_day")
    pod_game = {_gid(c) for c in candidates[:1]} if play_of_day else set()

    # Top 5 Moneylines (exclude the headline game to avoid exact duplication).
    moneylines: list[dict[str, Any]] = _take(5, "moneyline", exclude=pod_game)

    # Top 5 F5 Moneylines (same games as ML are fine — different market).
    f5: list[dict[str, Any]] = _take(5, "f5_moneyline", exclude=pod_game)

    # Top 3 Runlines — only when a meaningful edge exists (model "supports" a line).
    runlines: list[dict[str, Any]] = _take(3, "runline", require_min_edge=True, exclude=pod_game)

    # Safe Parlay: 2 safest ML/F5 picks (by confidence then edge), excluding headline.
    parlay_legs: list[dict[str, Any]] = []
    ranked_safe = sorted(
        [c for c in candidates if _gid(c) not in pod_game],
        key=lambda c: (str(c["confidence"] or ""), c["edge"]),
        reverse=True,
    )
    for cand in ranked_safe:
        if len(parlay_legs) >= 2:
            break
        leg = _make_pick(
            selected_date, "moneyline", cand["game"], cand["team"], cand["opponent"],
            cand["quant"], parlay_leg=True,
        )
        picks.append(leg)
        parlay_legs.append(leg)

    # Core Five: the next 5 best edges after the Top 5 Moneylines.
    core_five: list[dict[str, Any]] = _take(5, "moneyline", exclude=pod_game, skip=len(moneylines))

    card = {
        "source": SOURCE,
        "date": selected_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_mode": MARKET_MODE,
        "stats_only": stats_mode,
        "odds_status": "unavailable" if stats_mode else "available",
        "picks": picks,
        "parlay": [leg["id"] for leg in parlay_legs],
        "sections": {
            "play_of_day": [p["id"] for p in play_of_day],
            "moneylines": [p["id"] for p in moneylines],
            "f5": [p["id"] for p in f5],
            "runlines": [p["id"] for p in runlines],
            "core_five": [p["id"] for p in core_five],
        },
        "counts": {
            "play_of_day": len(play_of_day),
            "moneyline": len(moneylines),
            "f5_moneyline": len(f5),
            "runline": len(runlines),
            "parlay_legs": len(parlay_legs),
            "core_five": len(core_five),
            "total": len(picks),
        },
        "errors": errors,
    }
    return card


def save_simple_mlb_card(card: dict) -> str:
    """Persist a simple card to /data/simple_cards/YYYY-MM-DD.json."""
    card_date = card.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIMPLE_CARD_DIR / f"{card_date}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(card, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return str(path)


# Market types gradable by the results vault. play_of_day is a moneyline lean,
# so it is bridged as a moneyline while retaining its category for reporting.
_BRIDGE_MARKET_TYPE = {
    "play_of_day": "moneyline",
    "moneyline": "moneyline",
    "f5_moneyline": "f5_moneyline",
    "runline": "runline",
}


def export_simple_card_to_official_picks(card_date: str | None = None) -> dict:
    """Bridge a saved simple card into the normal official picks store.

    Loads ``/data/simple_cards/YYYY-MM-DD.json``, converts each pick into the
    normalized tracker format, and appends new picks to ``picks.json`` without
    overwriting or duplicating existing (StructuredCard) data.
    """
    from results_tracker import (
        PICKS_FILE,
        _normalize_saved_pick,
        _official_pick_id,
        _write_json,
        load_picks,
    )

    selected_date = card_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIMPLE_CARD_DIR / f"{selected_date}.json"
    result: dict[str, Any] = {
        "date": selected_date,
        "exists": path.exists(),
        "imported": 0,
        "skipped": 0,
        "simple_card_path": str(path),
        "path": str(PICKS_FILE),
        "error": None,
    }
    if not path.exists():
        result["error"] = "simple card not found"
        return result

    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:  # pragma: no cover - corrupt file
        result["error"] = f"simple card unreadable: {error!r}"
        return result

    raw_picks = card.get("all_picks") or card.get("picks") or []
    try:
        existing = load_picks()
    except Exception as error:  # pragma: no cover - storage unavailable
        result["error"] = f"picks store unreadable: {error!r}"
        return result

    existing_ids = {p.get("pick_id") for p in existing if isinstance(p, dict)}
    def bridge_key(pick: dict[str, Any], *, include_source: bool = True) -> tuple[str, ...]:
        market = str(pick.get("market_type") or pick.get("market") or "").lower()
        if market == "play_of_day":
            market = "moneyline"
        values = (
            str(pick.get("card_date") or pick.get("date") or ""),
            str(pick.get("sport") or "mlb").lower(),
            str(pick.get("game_pk") or pick.get("game_id") or ""), market,
            str(pick.get("selected_team") or pick.get("team") or "").lower(),
            str(pick.get("line") if pick.get("line") is not None else pick.get("market_line") or ""),
        )
        return values + ((str(pick.get("source") or ""),) if include_source else ())
    existing_contract_keys = {bridge_key(pick) for pick in existing if isinstance(pick, dict)}
    simple_underlying_keys = {
        bridge_key(pick, include_source=False) for pick in existing
        if isinstance(pick, dict) and pick.get("source") == "simple_mlb_card_v1"
    }
    imported: list[dict[str, Any]] = []
    skipped = 0

    for sp in raw_picks:
        if not isinstance(sp, dict):
            continue
        market = str(sp.get("market") or "")
        market_type = _BRIDGE_MARKET_TYPE.get(market, market)
        if market in {"parlay", "safe_parlay", "parlay_leg"} or sp.get("parlay_leg"):
            skipped += 1
            continue
        if not sp.get("team") or not sp.get("game_id"):
            skipped += 1
            continue
        norm: dict[str, Any] = {
            "card_date": sp.get("date") or selected_date,
            "date": sp.get("date") or selected_date,
            "game_pk": sp.get("game_id"),
            "game_id": sp.get("game_id"),
            "market_type": market_type,
            "market": market_type,
            "category": market or market_type,
            "selected_team": sp.get("team"),
            "selection": sp.get("team"),
            "opponent": sp.get("opponent"),
            "line": sp.get("posted_line"),
            "market_line": sp.get("posted_line"),
            "edge_score": sp.get("edge_score"),
            "confidence": sp.get("confidence"),
            "sportsbook": sp.get("sportsbook", "none"),
            "odds_status": sp.get("odds_status", "unavailable"),
            "market_mode": "stats_only",
            "trackable": True,
            "source": "simple_mlb_card_v1",
            "snapshot_source": "simple_card_bridge",
            "status": "pending",
            "result": None,
            "sport": "mlb",
            "league": "MLB",
            "model_version": sp.get("model_version", "BETGPTAI v21.0"),
            "parlay_leg": bool(sp.get("parlay_leg", False)),
            "game_time": sp.get("game_time"),
        }
        _normalize_saved_pick(norm)
        pid = norm.get("pick_id") or _official_pick_id(norm)
        norm["pick_id"] = pid
        contract_key = bridge_key(norm)
        underlying_key = bridge_key(norm, include_source=False)
        if pid in existing_ids or contract_key in existing_contract_keys or underlying_key in simple_underlying_keys:
            skipped += 1
            continue
        existing_ids.add(pid)
        existing_contract_keys.add(contract_key)
        simple_underlying_keys.add(underlying_key)
        imported.append(norm)

    if imported:
        _write_json(PICKS_FILE, existing + imported)

    result["imported"] = len(imported)
    result["skipped"] = skipped
    return result


def simple_card_bridge_status(card_date: str | None = None) -> dict:
    """Return diagnostic info about the simple card and its bridge state."""
    from results_tracker import load_picks

    selected_date = card_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = SIMPLE_CARD_DIR / f"{selected_date}.json"
    status: dict[str, Any] = {
        "date": selected_date,
        "simple_card_exists": path.exists(),
        "simple_pick_count": 0,
        "bridged": False,
        "results_vault_compatible": False,
        "errors": [],
    }
    if not path.exists():
        return status
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
        raw_picks = card.get("all_picks") or card.get("picks") or []
        status["simple_pick_count"] = len(raw_picks)
        # Compatible when every pick maps to a vault-gradable market type.
        compatible = all(
            isinstance(p, dict) and bool(p.get("team")) and bool(p.get("game_id"))
            and _BRIDGE_MARKET_TYPE.get(str(p.get("market") or ""), str(p.get("market") or ""))
            in {"moneyline", "f5_moneyline", "runline"}
            for p in raw_picks
        )
        status["results_vault_compatible"] = bool(raw_picks) and compatible
    except Exception as error:  # pragma: no cover - corrupt file
        status["errors"].append(f"simple card unreadable: {error!r}")
        return status

    try:
        picks = load_picks()
        status["bridged"] = any(
            isinstance(p, dict)
            and p.get("source") == "simple_mlb_card_v1"
            and str(p.get("card_date") or p.get("date")) == selected_date
            for p in picks
        )
    except Exception as error:  # pragma: no cover - storage unavailable
        status["errors"].append(f"picks store unreadable: {error!r}")
    return status


def _display_date(card_date: str) -> str:
    try:
        return datetime.strptime(card_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        return str(card_date)


def render_simple_mlb_card(card: dict) -> str:
    """Render clean Telegram text for the simple stats-based card."""
    by_id = {p["id"]: p for p in card.get("picks", [])}
    sections = card.get("sections", {})
    lines: list[str] = []
    lines.append("🏆 BETGPTAI MLB CARD")
    lines.append(f"📅 {_display_date(card.get('date', ''))}")
    lines.append(f"Mode: {'Stats Only' if card.get('stats_only') else 'Normal'}")
    lines.append("")

    def _block(title: str, ids: list[str]) -> None:
        if not ids:
            return
        lines.append(title)
        for idx, pid in enumerate(ids, start=1):
            pick = by_id.get(pid)
            if not pick:
                continue
            label = pick.get("team") or pick.get("pick") or "Unknown"
            market = pick.get("market", "")
            suffix = f" {market}" if market not in ("play_of_day",) else ""
            lines.append(f"{idx}. {label}{suffix}")
        lines.append("")

    _block("🔥 PLAY OF THE DAY", sections.get("play_of_day", []))
    _block("🏆 TOP MONEYLINES", sections.get("moneylines", []))
    _block("🔥 TOP F5", sections.get("f5", []))

    runlines = sections.get("runlines", [])
    if runlines:
        _block("📈 TOP RUNLINES", runlines)

    parlay_ids = card.get("parlay", [])
    if parlay_ids:
        lines.append("🧩 SAFE PARLAY")
        for idx, pid in enumerate(parlay_ids, start=1):
            pick = by_id.get(pid)
            label = pick.get("team") or "Unknown" if pick else "Unknown"
            lines.append(f"Leg {idx}: {label}")
        lines.append("")

    _block("⚾ CORE FIVE", sections.get("core_five", []))

    lines.append("Stats-based card. Odds vary by sportsbook. Verify lines before placing any wager.")
    return "\n".join(lines).strip()


def _resolve_destination(channel: str) -> int | str:
    """Resolve a channel env value or raw id to a Telegram destination."""
    cleaned = (channel or "").strip()
    if not cleaned:
        raise ValueError("Channel destination is empty.")
    if cleaned.startswith("@"):
        return cleaned
    try:
        numeric = int(cleaned)
    except ValueError as error:
        raise ValueError("Channel must be numeric or an @username.") from error
    if numeric > 0 and cleaned.startswith("100"):
        numeric = -numeric
    return numeric


async def post_simple_mlb_card(card: dict, bot: Any, channel_id: str) -> bool:
    """Post a rendered simple card to a resolved Telegram channel id.

    ``bot`` is the async Telegram bot and ``channel_id`` must already be resolved
    (e.g. via ``_resolve_destination``).  Returns True if at least one message sent.
    """
    if bot is None:
        return False
    text = render_simple_mlb_card(card)
    remaining = text.strip()
    posted = False
    while remaining:
        if len(remaining) <= 3900:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n\n", 0, 3900)
            if split_at < 1:
                split_at = remaining.rfind("\n", 0, 3900)
            if split_at < 1:
                split_at = 3900
            chunk, remaining = remaining[:split_at], remaining[split_at:].lstrip()
        await bot.send_message(
            chat_id=channel_id,
            text=chunk,
            parse_mode=None,
            disable_web_page_preview=True,
        )
        posted = True
    return posted
