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
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from storage import DATA_DIR, data_file
from services.daily_snapshot import load_snapshot, snapshot_status

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
RESULTS_ROOT = DATA_DIR / "results"
VAULT_LOG = DATA_DIR / "logs" / "vault.log"
VAULT_LOG.parent.mkdir(parents=True, exist_ok=True)
MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
REQUEST_TIMEOUT = 20


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
        if odds_float == 0:
            return 0.0
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
    if "prop" in mt or str(pick.get("category") or "").lower() == "approved_player_prop" or mt in {
        "hits", "home_runs", "total_bases", "rbis", "runs", "strikeouts",
        "pitcher_strikeouts", "player_hits", "player_total_bases", "player_home_runs",
    }:
        return "prop"
    return mt


def _is_parlay_leg(pick: dict[str, Any]) -> bool:
    return bool(pick.get("parlay_leg")) or str(pick.get("category") or "").lower() == "parlay_leg"


def _line(pick: dict[str, Any]) -> float | None:
    for key in ("posted_line", "line", "market_line"):
        value = pick.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            pass
    return None


def _stable_key(pick: dict[str, Any], *, include_source: bool = True) -> tuple[str, ...]:
    market = _market_from_pick(pick)
    if market == "play_of_day":
        market = "moneyline"
    sport = str(pick.get("sport") or "mlb").lower()
    game_id = str(pick.get("game_pk") or pick.get("game_id") or pick.get("match_id") or pick.get("fixture_id") or "")
    selected = re.sub(r"[^a-z0-9]", "", str(pick.get("selected_team") or pick.get("selection") or pick.get("pick_text") or "").lower())
    parts = [str(pick.get("card_date") or pick.get("date") or ""), sport, game_id, market, selected, str(_line(pick) or "")]
    if include_source:
        parts.append(str(pick.get("source") or pick.get("source_command") or ""))
    return tuple(parts)


def _dedupe_picks(picks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deduplicate stable IDs and semantic picks; retain play-of-day category."""
    unique: list[dict[str, Any]] = []
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    seen_semantic: dict[tuple[str, ...], int] = {}
    for pick in picks:
        if _is_parlay_leg(pick) or _market_from_pick(pick) == "parlay":
            continue
        pid = str(pick.get("pick_id") or "")
        semantic = _stable_key(pick, include_source=False)
        duplicate_index = seen_semantic.get(semantic)
        if (pid and pid in seen_ids) or duplicate_index is not None:
            groups.setdefault(semantic, [unique[duplicate_index] if duplicate_index is not None else pick]).append(pick)
            if duplicate_index is not None and _market_from_pick(pick) == "play_of_day":
                replacement = dict(pick)
                replacement["category"] = "play_of_day"
                unique[duplicate_index] = replacement
            continue
        if pid:
            seen_ids.add(pid)
        seen_semantic[semantic] = len(unique)
        unique.append(pick)
    return unique, [{"key": "|".join(key), "count": len(group), "picks": group} for key, group in groups.items()]


def _mlb_games(card_date: str, f5_ids: set[int]) -> dict[int, dict[str, Any]]:
    response = requests.get(MLB_SCHEDULE_URL, params={"sportId": "1", "date": card_date}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    games: dict[int, dict[str, Any]] = {}
    from results_tracker import _fetch_f5_score
    for date_group in payload.get("dates", []):
        for raw in date_group.get("games", []):
            game_id = raw.get("gamePk")
            if not isinstance(game_id, int):
                continue
            away = raw.get("teams", {}).get("away", {})
            home = raw.get("teams", {}).get("home", {})
            status = raw.get("status", {})
            game = {
                "game_pk": game_id, "game_id": game_id,
                "away_team": away.get("team", {}).get("name"), "home_team": home.get("team", {}).get("name"),
                "away_score": away.get("score"), "home_score": home.get("score"),
                "status": status.get("detailedState") or status.get("abstractGameState"),
                "final": status.get("abstractGameState") == "Final",
            }
            if game["final"] and game_id in f5_ids:
                game.update(_fetch_f5_score(game_id) or {})
            games[game_id] = game
    return games


def _match_game(pick: dict[str, Any], games: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    from results_tracker import _teams_match
    try:
        game_id = int(str(pick.get("game_pk") or pick.get("game_id") or ""))
    except ValueError:
        game_id = 0
    if game_id and game_id in games:
        return games[game_id]
    away, home = str(pick.get("away_team") or ""), str(pick.get("home_team") or "")
    if away and home:
        return next((game for game in games.values() if _teams_match(away, str(game.get("away_team") or "")) and _teams_match(home, str(game.get("home_team") or ""))), None)
    return None


def _grade_mlb_pick(pick: dict[str, Any], game: dict[str, Any]) -> tuple[str | None, str | None]:
    from results_tracker import _teams_match
    if not game.get("final"):
        return None, None
    market = _market_from_pick(pick)
    selected = str(pick.get("selected_team") or pick.get("selection") or "")
    away_selected = _teams_match(selected, str(game.get("away_team") or ""))
    home_selected = _teams_match(selected, str(game.get("home_team") or ""))
    if market in {"moneyline", "play_of_day", "runline", "f5", "team_total"} and not (away_selected or home_selected):
        return None, "final_score_not_found"
    away_score, home_score = game.get("away_score"), game.get("home_score")
    if not isinstance(away_score, int) or not isinstance(home_score, int):
        return None, "final_score_not_found"
    selected_score, opponent_score = (away_score, home_score) if away_selected else (home_score, away_score)
    if market in {"moneyline", "play_of_day"}:
        return ("win" if selected_score > opponent_score else "loss"), None
    if market == "f5":
        f5_away, f5_home = game.get("f5_away_score"), game.get("f5_home_score")
        if not isinstance(f5_away, int) or not isinstance(f5_home, int):
            return None, "final_score_not_found"
        selected_f5, opponent_f5 = (f5_away, f5_home) if away_selected else (f5_home, f5_away)
        return ("push" if selected_f5 == opponent_f5 else "win" if selected_f5 > opponent_f5 else "loss"), None
    if market == "runline":
        line = _line(pick)
        if line is None:
            return None, "line_missing"
        adjusted = selected_score + line
        return ("push" if adjusted == opponent_score else "win" if adjusted > opponent_score else "loss"), None
    if market == "game_total":
        line = _line(pick)
        if line is None:
            return None, "line_missing"
        total = away_score + home_score
        direction = str(pick.get("direction") or pick.get("selection") or pick.get("pick_text") or "").lower()
        if not (direction.startswith("over") or direction.startswith("under")):
            return None, "unsupported_market_type"
        return ("push" if total == line else "win" if (total > line) == direction.startswith("over") else "loss"), None
    if market == "team_total":
        line = _line(pick)
        if line is None:
            return None, "line_missing"
        direction = str(pick.get("direction") or pick.get("selection") or pick.get("pick_text") or "").lower()
        if "over" not in direction and "under" not in direction:
            return None, "unsupported_market_type"
        return ("push" if selected_score == line else "win" if (selected_score > line) == ("over" in direction) else "loss"), None
    if market == "prop":
        return None, "player_stat_missing"
    return None, "unsupported_market_type"


def grade_snapshot_date(card_date: str) -> dict[str, Any]:
    """Grade and persist one vault date using final MLB schedule results."""
    snapshot = load_snapshot(card_date)
    if not snapshot:
        return {"success": False, "error": "No official snapshot saved for this date."}
    return repair_results_date(card_date, cleanup_duplicates=False)


def repair_results_date(card_date: str, *, cleanup_duplicates: bool = True) -> dict[str, Any]:
    """Safely re-grade final MLB picks and optionally remove exact pending duplicates."""
    from results_tracker import FINAL_RESULTS, load_picks, rebuild_results
    from services.pick_persistence import write_picks_payload
    all_picks = load_picks()
    todays = [pick for pick in all_picks if str(pick.get("card_date") or pick.get("date") or "") == card_date]
    unique, duplicate_groups = _dedupe_picks(todays)
    f5_ids = {
        int(str(pick.get("game_pk") or pick.get("game_id"))) for pick in unique
        if _market_from_pick(pick) == "f5" and str(pick.get("game_pk") or pick.get("game_id") or "").isdigit()
    }
    try:
        games = _mlb_games(card_date, f5_ids)
    except Exception as error:
        logger.exception("MLB final games unavailable for %s", card_date)
        return {"success": False, "error": f"MLB final games unavailable: {error}"}
    matched = graded = pending = ungraded = 0
    ungraded_reasons: Counter[str] = Counter()
    samples = {"graded": [], "pending": [], "ungraded": []}
    for pick in unique:
        if str(pick.get("sport") or "mlb").lower() != "mlb":
            pending += 1
            samples["pending"].append(pick)
            continue
        game = _match_game(pick, games)
        if game:
            matched += 1
        if not game:
            reason = "missing_game_pk" if not (pick.get("game_pk") or pick.get("game_id") or (pick.get("away_team") and pick.get("home_team"))) else "final_score_not_found"
            if games and all(item.get("final") for item in games.values()):
                pick.update({"status": "ungraded", "result": None, "ungraded_reason": reason, "last_grading_error": reason})
                ungraded += 1
                ungraded_reasons[reason] += 1
                samples["ungraded"].append(pick)
            else:
                pending += 1
                samples["pending"].append(pick)
            continue
        result, reason = _grade_mlb_pick(pick, game)
        if result in FINAL_RESULTS:
            pick.update({"status": "graded", "result": result, "graded_at": _now_iso(), "ungraded_reason": None})
            pick["profit_units"] = _units_won(result, pick.get("odds"), pick.get("units") or pick.get("units_risked", 1))
            graded += 1
            samples["graded"].append(pick)
        elif reason:
            pick.update({"status": "ungraded", "result": None, "ungraded_reason": reason, "last_grading_error": reason})
            ungraded += 1
            ungraded_reasons[reason] += 1
            samples["ungraded"].append(pick)
        else:
            pick.update({"status": "pending", "result": None})
            pending += 1
            samples["pending"].append(pick)

    duplicates_removed = 0
    if cleanup_duplicates:
        seen_ids: set[str] = set()
        seen_exact: set[tuple[str, ...]] = set()
        cleaned: list[dict[str, Any]] = []
        for pick in all_picks:
            if str(pick.get("card_date") or pick.get("date") or "") != card_date:
                cleaned.append(pick)
                continue
            pid = str(pick.get("pick_id") or "")
            exact = _stable_key(pick, include_source=True)
            if pick.get("result") in FINAL_RESULTS:
                if pid:
                    seen_ids.add(pid)
                seen_exact.add(exact)
                cleaned.append(pick)
                continue
            if (pid and pid in seen_ids) or exact in seen_exact or _is_parlay_leg(pick) or _market_from_pick(pick) == "parlay":
                duplicates_removed += 1
                continue
            if pid:
                seen_ids.add(pid)
            seen_exact.add(exact)
            cleaned.append(pick)
        all_picks = cleaned
    write_picks_payload(all_picks, event="repair_results" if cleanup_duplicates else "vault_grade_results")
    rebuild_results(all_picks)
    snapshot = load_snapshot(card_date) or {}
    record = _build_results_record(card_date, unique, snapshot, duplicate_groups=duplicate_groups)
    path = _results_path(card_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)
    _log_vault("results_saved", card_date, str(path))

    return {
        "success": True, "path": str(path), "record": record,
        "date": card_date, "total_picks": len(todays), "duplicate_groups": duplicate_groups,
        "duplicates_removed": duplicates_removed, "final_mlb_games_found": sum(1 for game in games.values() if game.get("final")),
        "matched_picks": matched, "graded_picks": graded, "pending_picks": pending,
        "ungraded_picks": ungraded, "ungraded_reasons": dict(ungraded_reasons),
        "samples": {key: value[:5] for key, value in samples.items()},
    }


def results_debug_payload(card_date: str) -> dict[str, Any]:
    """Read-only result audit for owner diagnostics."""
    from results_tracker import load_picks
    todays = [json.loads(json.dumps(pick, default=str)) for pick in load_picks() if str(pick.get("card_date") or pick.get("date") or "") == card_date]
    unique, duplicate_groups = _dedupe_picks(todays)
    f5_ids = {
        int(str(pick.get("game_pk") or pick.get("game_id"))) for pick in unique
        if _market_from_pick(pick) == "f5" and str(pick.get("game_pk") or pick.get("game_id") or "").isdigit()
    }
    games = _mlb_games(card_date, f5_ids)
    matched = graded = pending = ungraded = 0
    reasons: Counter[str] = Counter()
    samples = {"graded": [], "pending": [], "ungraded": []}
    for pick in unique:
        if str(pick.get("sport") or "mlb").lower() != "mlb":
            pending += 1; samples["pending"].append(_result_pick_label(pick)); continue
        game = _match_game(pick, games)
        if game:
            matched += 1
            result, reason = _grade_mlb_pick(pick, game)
        else:
            result = None
            reason = "final_score_not_found" if games and all(item.get("final") for item in games.values()) else None
        if result:
            graded += 1; samples["graded"].append(f"{_result_pick_label(pick)} => {result}")
        elif reason:
            ungraded += 1; reasons[reason] += 1; samples["ungraded"].append(f"{_result_pick_label(pick)} => {reason}")
        else:
            pending += 1; samples["pending"].append(_result_pick_label(pick))
    return {
        "date": card_date, "total_picks": len(todays), "duplicate_groups": duplicate_groups,
        "final_mlb_games_found": sum(1 for game in games.values() if game.get("final")),
        "matched_picks": matched, "graded_picks": graded, "pending_picks": pending,
        "ungraded_picks": ungraded, "ungraded_reasons": dict(reasons), "samples": samples,
    }


def render_results_debug(payload: dict[str, Any]) -> str:
    samples = payload.get("samples") or {}
    lines = [
        "🧪 RESULTS DEBUG", f"Date: {payload.get('date')}",
        f"Total picks: {payload.get('total_picks', 0)}",
        f"Duplicate pick groups: {len(payload.get('duplicate_groups') or [])}",
        f"Final MLB games found: {payload.get('final_mlb_games_found', 0)}",
        f"Matched picks: {payload.get('matched_picks', 0)}",
        f"Graded picks: {payload.get('graded_picks', 0)}",
        f"Pending picks: {payload.get('pending_picks', 0)}",
        f"Ungraded picks: {payload.get('ungraded_picks', 0)}",
        f"Ungraded reasons: {payload.get('ungraded_reasons') or 'None'}",
    ]
    for key in ("graded", "pending", "ungraded"):
        lines.extend(["", f"Sample {key}:"])
        lines.extend(f"- {item}" for item in (samples.get(key) or [])[:5])
        if not samples.get(key):
            lines.append("- None")
    return "\n".join(lines)


def _build_results_record(card_date: str, graded_picks: list[dict[str, Any]], snapshot: dict[str, Any], *, duplicate_groups: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    ungraded_count = 0
    manual_review: list[str] = []
    pending_list: list[str] = []

    for market, market_picks in market_groups.items():
        wins = sum(1 for p in market_picks if p.get("result") == "win")
        losses = sum(1 for p in market_picks if p.get("result") == "loss")
        pushes = sum(1 for p in market_picks if p.get("result") == "push")
        pending = sum(1 for p in market_picks if p.get("status") == "pending")
        ungraded_market = sum(1 for p in market_picks if p.get("status") == "ungraded")
        units = sum(_units_won(p.get("result"), p.get("odds"), p.get("units") or p.get("units_risked", 1)) for p in market_picks)
        total_wins += wins
        total_losses += losses
        total_pushes += pushes
        pending_count += pending
        ungraded_count += ungraded_market
        total_units += units

        results_summary[market] = {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "pending": pending,
            "ungraded": ungraded_market,
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
                    "status": p.get("status"),
                    "ungraded_reason": p.get("ungraded_reason"),
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
    if any(p.get("status") == "pending" for p in graded_picks):
        pending_examples = [
            _result_pick_label(p)
            for p in graded_picks if p.get("status") == "pending"
        ]
        pending_list.extend(pending_examples[:10])
    ungraded_list = [_result_pick_label(p) for p in graded_picks if p.get("status") == "ungraded"]

    # CLV summary
    clv_picks = [p for p in graded_picks if p.get("clv") is not None]
    positive_clv = sum(1 for p in clv_picks if float(p.get("clv", 0)) > 0 and p.get("result") == "win")
    negative_clv = sum(1 for p in clv_picks if float(p.get("clv", 0)) <= 0 and p.get("result") == "win")
    no_clv = sum(1 for p in graded_picks if p.get("clv") is None)

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
        "clv_summary": {
            "picks_with_clv": len(clv_picks),
            "picks_without_clv": no_clv,
            "wins_with_positive_clv": positive_clv,
            "wins_with_negative_clv": negative_clv,
            "clv_unavailable_reason": "closing_line unavailable" if no_clv > 0 else None,
        },
        "market_records": results_summary,
        "pending": pending_list[:50],
        "pending_count": pending_count,
        "ungraded": ungraded_list[:50],
        "ungraded_count": ungraded_count,
        "duplicates_ignored": sum(max(0, int(group.get("count") or 0) - 1) for group in (duplicate_groups or [])),
        "duplicate_groups": duplicate_groups or [],
        "manual_review": manual_review,
        "snapshot_path": str(_results_path(card_date).parent / f"{card_date}.json"),
    }
    return record


def _result_pick_label(pick: dict[str, Any]) -> str:
    market = _market_from_pick(pick).replace("_", " ").upper()
    selection = pick.get("selected_team") or pick.get("selection") or pick.get("pick_text") or "Unknown selection"
    matchup = pick.get("match_name") or pick.get("fixture_name") or ""
    line = _line(pick)
    suffix = f" {line:g}" if line is not None else ""
    reason = pick.get("ungraded_reason") or pick.get("last_grading_error") or pick.get("status") or "pending"
    return f"{matchup + ' — ' if matchup else ''}{selection} {market}{suffix} — {reason}"


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
    if results and card_date < _today_str() and (results.get("pending") or results.get("pending_count", 0)):
        try:
            refreshed = repair_results_date(card_date, cleanup_duplicates=False)
            if refreshed.get("success"):
                results = refreshed.get("record") or results
        except Exception:
            logger.exception("Automatic historical result refresh failed for %s", card_date)
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
        f"Final graded: {overall.get('total', 0)}",
        f"Pending: {results.get('pending_count', 0)}",
        f"Ungraded: {results.get('ungraded_count', 0)}",
        f"Duplicates ignored: {results.get('duplicates_ignored', 0)}",
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
        elif rec.get("ungraded", 0) > 0:
            lines.append(f"{label}: {rec.get('ungraded')} ungraded")

    pending = results.get("pending") or []
    if pending:
        lines.extend(["", "Pending Picks:"])
        lines.extend(f"- {item}" for item in pending[:10])

    ungraded = results.get("ungraded") or []
    if ungraded:
        lines.extend(["", "Ungraded Final Picks:"])
        lines.extend(f"- {item}" for item in ungraded[:10])

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
