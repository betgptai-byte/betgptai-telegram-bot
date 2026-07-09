"""BETGPTAI Official Picks Builder — builds the official card from ranked candidates.

Selects:
- Play of the Day = highest edge score
- Core Five = top 5 unique high-confidence plays
- Safe Parlay = best 2 low-risk independent legs
- Top ML/RL/F5/Totals/TT by category

Rules:
- No official pick below 80 edge score.
- Do not force plays — if no candidate qualifies, market is omitted.
- Admin-only inferred lines excluded from official card.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_admin_only(candidate: dict[str, Any]) -> bool:
    return bool(candidate.get("admin_market_fallback") or candidate.get("inferred_line_admin_only"))


def _edge_score(candidate: dict[str, Any]) -> float:
    return _num(candidate.get("edge_score") or candidate.get("final_edge_score") or candidate.get("quant_edge_score"))


def _market_type(candidate: dict[str, Any]) -> str:
    mt = str(candidate.get("market_type") or candidate.get("market") or "").lower()
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
    if mt in {"player_prop", "prop", "hitter_prop", "pitcher_prop"}:
        return "prop"
    return mt


def _confidence_label(edge: float) -> str:
    if edge >= 92:
        return "Elite"
    if edge >= 87:
        return "Strong"
    if edge >= 82:
        return "Playable"
    return "Pass"


def _can_pair(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two candidates can form a parlay only if they are from different games."""
    pk_a = str(a.get("game_pk") or a.get("game_id") or "")
    pk_b = str(b.get("game_pk") or b.get("game_id") or "")
    return pk_a != pk_b


def build_official_card(
    candidates: list[dict[str, Any]],
    *,
    min_edge: float = 80.0,
    max_ml: int = 3,
    max_rl: int = 3,
    max_f5: int = 3,
    max_totals: int = 3,
    max_tt: int = 3,
    max_props: int = 5,
) -> dict[str, Any]:
    """Build official card from ranked candidate markets.

    Args:
        candidates: List of candidate picks to rank.
        min_edge: Minimum edge score to qualify.
        max_ml: Max moneyline picks.
        max_rl: Max runline picks.
        max_f5: Max F5 picks.
        max_totals: Max game total picks.
        max_tt: Max team total picks.
        max_props: Max official props.

    Returns:
        Dict with keys: play_of_day, moneylines, runlines, f5, game_totals,
        team_totals, core_five, safe_parlay, props, builder_notes.
    """
    # Remove admin-only inferred lines
    eligible = [c for c in candidates if not _is_admin_only(c)]
    if not eligible:
        return {
            "play_of_day": None,
            "moneylines": [],
            "runlines": [],
            "f5": [],
            "game_totals": [],
            "team_totals": [],
            "safe_parlay": None,
            "core_five": [],
            "props": [],
            "builder_notes": ["No eligible candidates — all were admin-only inferred lines."],
        }

    ranked = sorted(eligible, key=_edge_score, reverse=True)
    qualified = [c for c in ranked if _edge_score(c) >= min_edge]
    if not qualified:
        return {
            "play_of_day": None,
            "moneylines": [],
            "runlines": [],
            "f5": [],
            "game_totals": [],
            "team_totals": [],
            "safe_parlay": None,
            "core_five": [],
            "props": [],
            "builder_notes": [f"No candidates meet minimum edge score of {min_edge}."],
        }

    notes: list[str] = []
    by_market: dict[str, list[dict[str, Any]]] = {}
    for c in qualified:
        mt = _market_type(c)
        by_market.setdefault(mt, []).append(c)

    # Play of the Day = highest edge score
    play_of_day = qualified[0] if qualified else None
    if play_of_day:
        notes.append(f"Play of the Day: {play_of_day.get('pick_text') or play_of_day.get('selected_team')} (edge {_edge_score(play_of_day):.0f})")

    # By-market selections
    moneylines = by_market.get("moneyline", [])[:max_ml]
    runlines = by_market.get("runline", [])[:max_rl]
    f5 = by_market.get("f5", [])[:max_f5]
    game_totals = by_market.get("game_total", [])[:max_totals]
    team_totals = by_market.get("team_total", [])[:max_tt]
    props = by_market.get("prop", [])[:max_props]

    # Core Five = top 5 unique high-confidence plays (spread across markets)
    core_five = []
    seen_pk: set[str] = set()
    for c in qualified:
        if len(core_five) >= 5:
            break
        pk = str(c.get("game_pk") or c.get("game_id") or "")
        if pk and pk not in seen_pk:
            core_five.append(c)
            seen_pk.add(pk)
    if len(core_five) < 5:
        notes.append(f"Core Five only has {len(core_five)} unique-game candidates; {5 - len(core_five)} slot(s) unfilled.")

    # Safe Parlay = best 2 low-risk independent legs
    low_risk = [c for c in qualified if str(c.get("risk") or c.get("risk_level") or "").lower() in ("low", "moderate")]
    safe_parlay = None
    for i, a in enumerate(low_risk):
        for b in low_risk[i + 1:]:
            if _can_pair(a, b):
                safe_parlay = [
                    {"selection": a.get("pick_text") or a.get("selected_team"), "odds": a.get("odds"), "edge": _edge_score(a)},
                    {"selection": b.get("pick_text") or b.get("selected_team"), "odds": b.get("odds"), "edge": _edge_score(b)},
                ]
                break
        if safe_parlay:
            break

    return {
        "play_of_day": play_of_day,
        "moneylines": moneylines,
        "runlines": runlines,
        "f5": f5,
        "game_totals": game_totals,
        "team_totals": team_totals,
        "safe_parlay": safe_parlay,
        "core_five": core_five,
        "props": props,
        "builder_notes": notes,
    }
