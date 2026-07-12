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


def _draftkings_reference_detail(
    game: dict[str, Any], market: str, team: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Find the matching DK row or return an exact attachment failure."""
    from api.sharp_client import _normalize_team

    expected_market = {
        "play_of_day": "h2h", "moneyline": "h2h",
        "f5_moneyline": "f5_h2h", "runline": "spreads",
        "game_total": "totals", "team_total": "team_totals",
    }.get(market)
    if not expected_market:
        return None, "market_missing"
    prices = game.get("best_available_prices")
    context = game.get("market_context") if isinstance(game.get("market_context"), dict) else {}
    if not isinstance(prices, list) or not prices or not context.get("market_context_available"):
        return None, "game_not_matched"
    market_rows = [
        row for row in prices
        if isinstance(row, dict)
        and str(row.get("market") or "").lower() == expected_market
        and str(row.get("bookmaker_key") or row.get("bookmaker") or "").lower() == "draftkings"
    ]
    if not market_rows:
        return None, "f5_market_missing" if market == "f5_moneyline" else "market_missing"
    target = _normalize_team(team)
    selection_rows: list[dict[str, Any]] = []
    for row in market_rows:
        outcome = str(row.get("outcome") or row.get("description") or "")
        if expected_market not in {"totals"} and target not in _normalize_team(outcome):
            continue
        selection_rows.append(row)
    if not selection_rows:
        return None, "selection_missing"
    for row in selection_rows:
        if expected_market in {"spreads", "totals", "team_totals"} and row.get("point") is None:
            continue
        odds = row.get("price") if row.get("price") is not None else row.get("odds_american")
        if odds is None:
            continue
        return row, None
    if expected_market in {"spreads", "totals", "team_totals"} and all(row.get("point") is None for row in selection_rows):
        return None, "line_missing"
    return None, "odds_missing"


def _draftkings_reference(game: dict[str, Any], market: str, team: str) -> dict[str, Any] | None:
    reference, _ = _draftkings_reference_detail(game, market, team)
    return reference


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
    attach_debug: dict[str, Any] | None = None,
) -> dict[str, Any]:
    game_pk = game.get("game_pk") or game.get("game_id")
    edge = quant.get("final_edge_score")
    confidence = quant.get("confidence")
    reference, attach_reason = _draftkings_reference_detail(game, market, team)
    if attach_debug is not None:
        attach_debug["attempts"] += 1
        if reference:
            attach_debug["success"] += 1
        else:
            attach_debug["failures"] += 1
            reason = "market_missing" if attach_reason == "f5_market_missing" else str(attach_reason or "market_missing")
            attach_debug["failure_reasons"][reason] += 1
    reference_line = reference.get("point") if reference else line
    reference_odds = (
        reference.get("price") if reference and reference.get("price") is not None
        else reference.get("odds_american") if reference else None
    )
    verified = bool(reference and reference_odds is not None)
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
        "market_mode": "live_odds" if verified else "stats_only",
        "odds_status": "available" if verified else "unavailable",
        "sportsbook": "draftkings" if verified else "none",
        "odds_american": reference_odds,
        "posted_odds": reference_odds,
        "posted_line": reference_line,
        "line": reference_line,
        "line_verified": verified,
        "odds_attach_reason": None if verified else attach_reason,
        "trackable": True,
        "source": SOURCE,
        "parlay_leg": parlay_leg,
        "game_time": game.get("game_time"),
        "model_version": quant.get("model_version", "BETGPTAI v21.0"),
    }


def build_simple_mlb_card(card_date: str | None = None) -> dict:
    """Build a stats-only MLB card dict directly from the enriched slate."""
    from ai_analysis import upcoming_mlb_slate
    from api.sharp_odds_client import game_market_diagnostic
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
            odds_max_pages=10,
        )
    except Exception as error:  # pragma: no cover - network/IO dependent
        errors.append(f"slate_load_failed: {error!r}")

    dk_context_debug: dict[str, Any] = {
        "games_available": 0, "matched_games": 0, "source": "none", "rows_used": 0,
        "pages_fetched": 0, "pagination_truncated": False,
    }
    if raw_slate:
        try:
            dk_result = game_market_diagnostic()
            dk_context_debug.update({
                "games_available": int(dk_result.get("events_found") or dk_result.get("events_returned") or 0),
                "source": "paginated/full" if dk_result.get("accepted_game_market_rows") else "none",
                "rows_used": int(dk_result.get("accepted_game_market_rows") or 0),
                "pages_fetched": int(dk_result.get("pages_fetched") or 0),
                "pagination_truncated": bool(dk_result.get("pagination_truncated")),
            })
            dk_context_debug["matched_games"] = sum(
                1 for game in raw_slate
                if isinstance(game.get("market_context"), dict)
                and game["market_context"].get("market_context_available")
            )
        except Exception as error:  # pragma: no cover - network/rate dependent
            errors.append(f"full_dk_market_context_diagnostic_failed: {error!r}")

    slate = upcoming_mlb_slate(raw_slate) if raw_slate else []
    if not slate:
        errors.append("No upcoming MLB games available for card date.")

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

    edge_reports: list[dict[str, Any]] = []
    edge_engine_on = False
    try:
        from services.mlb_game_edge_engine import build_game_edge_reports
        edge_reports = build_game_edge_reports(slate)
        edge_engine_on = bool(edge_reports)
    except Exception as error:  # live-path fallback is intentional
        errors.append(f"game_edge_engine_failed_fallback_simple_scoring: {error!r}")
    reports_by_game = {
        str(report.get("game_pk") or report.get("game_id")): report
        for report in edge_reports
    }

    candidates: list[dict[str, Any]] = []
    for game in slate:
        quant = _quant_for(game)
        report = reports_by_game.get(str(game.get("game_pk") or game.get("game_id")))
        candidate_pick = report.get("official_pick_candidate") if isinstance(report, dict) and isinstance(report.get("official_pick_candidate"), dict) else {}
        team = str(candidate_pick.get("team") or "")
        if not team:
            team, opponent = _favored_side(game)
        else:
            opponent = str(game.get("home_team") if team == game.get("away_team") else game.get("away_team"))
        pick_quant = dict(quant)
        if isinstance(report, dict):
            pick_quant.update({
                "final_edge_score": report.get("overall_edge_score"),
                "confidence": report.get("confidence_grade"),
                "model_version": "mlb_game_edge_engine_v1",
            })
        candidates.append({
            "game": game,
            "team": team,
            "opponent": opponent,
            "quant": pick_quant,
            "edge": float(report.get("overall_edge_score") if isinstance(report, dict) else quant.get("final_edge_score") or 0.0),
            "confidence": report.get("confidence_grade") if isinstance(report, dict) else quant.get("confidence"),
            "risk": quant.get("risk_level"),
            "edge_report": report,
        })

    # Rank by edge score (highest first); fall back to insertion order.
    candidates.sort(key=lambda c: c["edge"], reverse=True)

    picks: list[dict[str, Any]] = []
    attach_debug: dict[str, Any] = {
        "attempts": 0, "success": 0, "failures": 0,
        "failure_reasons": {
            "game_not_matched": 0, "market_missing": 0,
            "selection_missing": 0, "line_missing": 0, "odds_missing": 0,
        },
    }

    def _gid(cand: dict[str, Any]) -> Any:
        return cand["game"].get("game_pk") or cand["game"].get("game_id")

    def _take(n: int, market: str, *, require_min_edge: bool = False,
              exclude: set[Any] | None = None, skip: int = 0) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        scanned = 0
        market_candidates = sorted(
            candidates,
            key=lambda cand: (
                True if market == "play_of_day" else str((cand.get("edge_report") or {}).get("best_market") or "") == market,
                {"qualified": 2, "watchlist": 1, "pass": 0}.get(str((cand.get("edge_report") or {}).get("qualification_status") or "pass"), 0),
                cand["edge"],
            ),
            reverse=True,
        )
        for cand in market_candidates:
            if len(out) >= n:
                break
            report = cand.get("edge_report") if isinstance(cand.get("edge_report"), dict) else {}
            if market == "play_of_day" and edge_engine_on:
                if report.get("qualification_status") != "qualified" or float(report.get("overall_edge_score") or 0) < 80:
                    continue
            if exclude and _gid(cand) in exclude:
                continue
            if require_min_edge and cand["edge"] <= 0:
                continue
            scanned += 1
            if scanned <= skip:
                continue
            picks.append(_make_pick(
                selected_date, market, cand["game"], cand["team"], cand["opponent"], cand["quant"],
                attach_debug=attach_debug,
            ))
            out.append(picks[-1])
        return out

    # Play of the Day: single highest-edge pick.
    play_of_day: list[dict[str, Any]] = _take(1, "play_of_day")
    pod_game = {play_of_day[0].get("game_id")} if play_of_day else set()

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
            cand["quant"], parlay_leg=True, attach_debug=attach_debug,
        )
        picks.append(leg)
        parlay_legs.append(leg)

    # Core Five: the next 5 best edges after the Top 5 Moneylines.
    core_five: list[dict[str, Any]] = _take(5, "moneyline", exclude=pod_game, skip=len(moneylines))

    verified_picks = [
        pick for pick in picks
        if pick.get("sportsbook") == "draftkings" and pick.get("line_verified")
        and (pick.get("odds_american") is not None or pick.get("posted_odds") is not None)
    ]
    live_mode = bool(verified_picks)
    watch_reports = [report for report in edge_reports if report.get("qualification_status") == "watchlist"]
    passed_reports = [
        report for report in edge_reports
        if report.get("qualification_status") == "pass"
    ]
    pass_games = len(passed_reports)
    qualified_reports = [report for report in edge_reports if report.get("qualification_status") == "qualified"]
    edge_warning = None
    if edge_engine_on and not qualified_reports and picks:
        edge_warning = "Game Edge Engine produced zero qualified picks; using simple fallback."
        errors.append(edge_warning)
    pass_reason_counts: dict[str, int] = {}
    for report in edge_reports:
        if report.get("qualification_status") != "pass":
            continue
        for reason in report.get("red_flags") or []:
            pass_reason_counts[str(reason)] = pass_reason_counts.get(str(reason), 0) + 1
    market_distribution = {
        market: sum(1 for report in edge_reports if report.get("best_market") == market)
        for market in ("moneyline", "f5_moneyline", "runline", "team_total", "game_total", "pass")
    }
    card = {
        "source": SOURCE,
        "date": selected_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_mode": "live_odds" if live_mode else "stats_only",
        "stats_only": not live_mode,
        "odds_status": "available" if live_mode else "unavailable",
        "game_market_book": "draftkings",
        "prop_book": "fanduel",
        "draftkings_lines_verified": live_mode,
        "fanduel_props_verified": False,
        "market_context_matched_games": dk_context_debug["matched_games"],
        "dk_market_context_games_available": dk_context_debug["games_available"],
        "dk_market_context_source": dk_context_debug["source"],
        "dk_market_context_rows_used": dk_context_debug["rows_used"],
        "dk_market_context_pages_fetched": dk_context_debug["pages_fetched"],
        "dk_market_context_pagination_truncated": dk_context_debug["pagination_truncated"],
        "dk_odds_attach_attempts": attach_debug["attempts"],
        "dk_odds_attach_success": attach_debug["success"],
        "dk_odds_attach_failures": attach_debug["failures"],
        "dk_odds_attach_failure_reasons": attach_debug["failure_reasons"],
        "game_edge_engine_on": edge_engine_on,
        "game_edge_reports_generated": len(edge_reports),
        "game_edge_pass_games": pass_games,
        "game_edge_watchlist": len(watch_reports),
        "game_edge_qualified_picks": len(qualified_reports),
        "game_edge_top": edge_reports[0] if edge_reports else None,
        "game_edge_market_distribution": market_distribution,
        "game_edge_pass_reason_counts": pass_reason_counts,
        "game_edge_warning": edge_warning,
        "game_edge_reports": edge_reports,
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
            "odds": sp.get("odds_american") if sp.get("odds_american") is not None else sp.get("posted_odds"),
            "posted_odds": sp.get("posted_odds") if sp.get("posted_odds") is not None else sp.get("odds_american"),
            "odds_status": sp.get("odds_status", "unavailable"),
            "line_verified": bool(sp.get("line_verified")),
            "market_mode": sp.get("market_mode", "stats_only"),
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
        return datetime.strptime(card_date, "%Y-%m-%d").strftime("%A, %m/%d/%Y")
    except (ValueError, TypeError):
        return str(card_date)


def _format_market_label(market_type: str) -> str:
    """Convert internal market keys into compact public-facing labels."""
    normalized = str(market_type or "").strip().lower()
    return {
        "play_of_day": "ML",
        "moneyline": "ML",
        "ml": "ML",
        "f5_moneyline": "F5 ML",
        "f5_ml": "F5 ML",
        "runline": "RL",
        "run_line": "RL",
        "game_total": "Game Total",
        "total": "Game Total",
        "team_total": "Team Total",
    }.get(normalized, "Pick")


def _format_odds(odds_american: Any) -> str:
    """Return signed American odds in parentheses, or an empty string."""
    if odds_american in (None, ""):
        return ""
    try:
        odds = int(float(odds_american))
    except (TypeError, ValueError):
        return ""
    return f"({odds:+d})"


def _format_line(market_type: str, posted_line: Any, direction: str | None = None) -> str:
    """Format a verified market line using public betting notation."""
    market = str(market_type or "").strip().lower()
    if posted_line in (None, ""):
        return _format_market_label(market)
    try:
        number = float(posted_line)
        unsigned = f"{number:g}"
        signed = f"{number:+g}"
    except (TypeError, ValueError):
        unsigned = signed = str(posted_line)
    side = str(direction or "").strip().title()
    if market in {"runline", "run_line"}:
        return f"RL {signed}"
    if market in {"game_total", "total"}:
        return f"{side or 'Total'} {unsigned}"
    if market == "team_total":
        return f"Team Total {side or 'Total'} {unsigned}"
    return _format_market_label(market)


def _has_verified_draftkings_lines(card: dict) -> bool:
    """True only when a public game pick carries a verified DK price."""
    picks = card.get("picks") if isinstance(card.get("picks"), list) else []
    public_markets = {"play_of_day", "moneyline", "f5_moneyline", "runline", "game_total", "team_total"}
    return any(
        isinstance(pick, dict)
        and str(pick.get("market_type") or pick.get("market") or "").lower() in public_markets
        and str(pick.get("sportsbook") or "").lower() == "draftkings"
        and bool(pick.get("line_verified"))
        and (pick.get("odds_american") is not None or pick.get("posted_odds") is not None)
        for pick in picks
    )


def _format_pick_public(pick: dict) -> str:
    """Render one simple-card pick without leaking technical market names."""
    selection = str(
        pick.get("selected_team") or pick.get("team") or pick.get("selection")
        or pick.get("pick") or "Pick unavailable"
    ).strip()
    market = str(pick.get("market_type") or pick.get("market") or "")
    normalized_market = market.strip().lower()
    direction = str(pick.get("direction") or "").strip().lower()
    if not direction and selection.lower() in {"over", "under"}:
        direction = selection.lower()
    if normalized_market in {"game_total", "total"} and selection.lower() in {"over", "under"}:
        selection = "Game Total"
    line = pick.get("posted_line")
    if line is None:
        line = pick.get("line")
    market_text = _format_line(market, line, direction)
    if normalized_market in {"game_total", "total"} and selection == "Game Total":
        market_text = market_text.removeprefix("Game Total ")
    odds = pick.get("odds_american")
    if odds is None:
        odds = pick.get("posted_odds")
    odds_text = _format_odds(odds) if (
        pick.get("line_verified") and str(pick.get("sportsbook") or "").lower() == "draftkings"
    ) else ""
    return " ".join(part for part in (selection, market_text, odds_text) if part).strip()


def render_simple_mlb_card(card: dict) -> str:
    """Render a polished, public-safe BETGPTAI free-channel card."""
    picks = [pick for pick in card.get("picks", []) if isinstance(pick, dict)]
    by_id = {str(pick.get("id")): pick for pick in picks if pick.get("id")}
    sections = card.get("sections", {}) if isinstance(card.get("sections"), dict) else {}
    has_dk_lines = _has_verified_draftkings_lines(card)
    mode_label = "DraftKings Reference Lines" if has_dk_lines else "Stats-Based"
    game_book = "DraftKings" if has_dk_lines else "Not matched"
    prop_book = "FanDuel"
    separator = "━━━━━━━━━━━━━━"
    number_icons = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
    blocks: list[list[str]] = [[
        "🏆 BETGPTAI MLB CARD",
        f"📅 {_display_date(card.get('date', ''))}",
        "⚾ MLB Free Picks",
        f"📊 Mode: {mode_label}",
    ]]
    book_labels = {"draftkings": "DraftKings", "fanduel": "FanDuel"}
    blocks[0].append(f"📚 Game Lines: {book_labels.get(game_book.lower(), game_book)}")
    blocks[0].append(f"🎯 Props: {book_labels.get(prop_book.lower(), prop_book)}")

    def _unique(ids: list[str]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for pid in ids or []:
            pick = by_id.get(str(pid))
            if not pick:
                continue
            key = (
                str(pick.get("selected_team") or pick.get("team") or pick.get("selection") or "").casefold(),
                str(pick.get("market_type") or pick.get("market") or "").casefold(),
                str(pick.get("posted_line") if pick.get("posted_line") is not None else pick.get("line") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(pick)
        return unique

    def _block(title: str, ids: list[str], *, headline: bool = False, checkmarks: bool = False) -> None:
        section_picks = _unique(ids)
        if not section_picks:
            return
        block = [title]
        for index, pick in enumerate(section_picks[:10]):
            prefix = "⭐" if headline else "✅" if checkmarks else number_icons[index]
            block.append(f"{prefix} {_format_pick_public(pick)}")
        blocks.append(block)

    _block("🔥 PLAY OF THE DAY", sections.get("play_of_day", []), headline=True)
    _block("🏆 TOP MONEYLINES", sections.get("moneylines", []))
    _block("🔥 FIRST 5 INNINGS", sections.get("f5", []))
    _block("📈 RUN LINE LEANS", sections.get("runlines", []))
    _block("🧩 SAFE 2-LEG PARLAY", card.get("parlay", []), checkmarks=True)
    _block("⚾ CORE FIVE", sections.get("core_five", []))
    reminder = (
        "DraftKings lines used as market reference. Odds may move."
        if has_dk_lines else
        "Stats-based card. Odds vary by sportsbook."
    )
    blocks.append([
        "⚠️ Reminder:",
        reminder,
        "Verify final lines and lineups before placing any wager.",
        "",
        "@betgptai",
    ])
    rendered = f"\n\n{separator}\n\n".join("\n".join(block).rstrip() for block in blocks)
    return rendered[:4096].rstrip()


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
