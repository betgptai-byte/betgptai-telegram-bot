"""BETGPTAI Bullpen Engine v2 — fatigue, availability, and late-inning risk.

Uses MLB Stats API and enriched slate data to calculate:
- last 3 days bullpen usage
- last 7 days bullpen usage
- relievers used yesterday
- back-to-back relievers
- closer availability
- setup availability
- bullpen fatigue score 0–100
- late inning risk
"""

from __future__ import annotations

import logging
from typing import Any

from edge_database import clamp

logger = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace("%", "").replace(" mph", ""))
    except (TypeError, ValueError):
        return default


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _bullpen_recent_usage(game: dict[str, Any], side: str, days: int) -> dict[str, Any]:
    """Extract recent bullpen usage for one side over *days*."""
    savant = _dict(game.get("savant"))
    pen = _dict(savant.get(f"{side}_bullpen"))
    usage_key = f"relievers_last_{days}d" if days <= 3 else "relievers_last_7d"
    usage = _list(pen.get(usage_key))
    relievers_used = len(usage)
    total_pitches = sum(int(r.get("pitches", 0)) for r in usage if isinstance(r, dict))
    return {
        "relievers_used": relievers_used,
        "total_pitches": total_pitches,
        "avg_pitches_per_reliever": round(total_pitches / max(relievers_used, 1), 1),
    }


def _yesterday_usage(game: dict[str, Any], side: str) -> dict[str, Any]:
    """Relievers who pitched yesterday (back-to-back risk)."""
    savant = _dict(game.get("savant"))
    pen = _dict(savant.get(f"{side}_bullpen"))
    yesterday = _list(pen.get("relievers_used_yesterday"))
    return {
        "b2b_candidates": len(yesterday),
        "relievers": [r.get("name", "?") for r in yesterday[:5] if isinstance(r, dict)],
    }


def _closer_availability(game: dict[str, Any], side: str) -> dict[str, Any]:
    """Closer and setup availability."""
    savant = _dict(game.get("savant"))
    pen = _dict(savant.get(f"{side}_bullpen"))
    closer = str(pen.get("closer") or pen.get("closer_available") or UNAVAILABLE)
    setup = str(pen.get("setup") or pen.get("setup_available") or UNAVAILABLE)
    return {
        "closer": closer,
        "setup": setup,
        "closer_available": closer.lower() not in (UNAVAILABLE, "false", "no", "none", ""),
        "setup_available": setup.lower() not in (UNAVAILABLE, "false", "no", "none", ""),
    }


def _fatigue_score(
    recent_3d: dict[str, Any],
    recent_7d: dict[str, Any],
    yesterday: dict[str, Any],
    closer: dict[str, Any],
) -> float:
    """Calculate bullpen fatigue 0–100. Higher = more fatigued."""
    score = 30.0
    # 3-day volume
    r3_relievers = recent_3d.get("relievers_used", 0)
    r3_pitches = recent_3d.get("total_pitches", 0)
    score += r3_relievers * 3
    score += r3_pitches * 0.5
    # Back-to-back penalty
    b2b = yesterday.get("b2b_candidates", 0)
    score += b2b * 5
    # Closer fatigue (if closer used recently, add penalty)
    if not closer.get("closer_available", True):
        score += 10
    # 7-day total volume check
    r7_pitches = recent_7d.get("total_pitches", 0)
    if r7_pitches > 150:
        score += 10
    elif r7_pitches > 100:
        score += 5
    return clamp(score, 0, 100)


def _late_inning_risk(
    fatigue: float,
    closer: dict[str, Any],
    setup: dict[str, Any],
    penalty: float = 0.0,
) -> float:
    """Score late-inning risk 0–100 based on fatigue and availability."""
    risk = fatigue * 0.5
    if not closer.get("closer_available", True):
        risk += 20
    if not setup.get("setup_available", True):
        risk += 10
    # Penalty passed from opposing team analysis
    risk += penalty
    return clamp(risk, 0, 100)


def bullpen_score_v2(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score bullpen edge 0–100 for both teams with fatigue and availability.

    Returns:
        (edge_score, details_dict)
    """
    details: dict[str, Any] = {}
    available = 0
    possible = 12

    side_details = {}
    for side in ("away", "home"):
        recent_3d = _bullpen_recent_usage(game, side, 3)
        recent_7d = _bullpen_recent_usage(game, side, 7)
        yesterday = _yesterday_usage(game, side)
        closer = _closer_availability(game, side)
        fatigue = _fatigue_score(recent_3d, recent_7d, yesterday, closer)
        late_risk = _late_inning_risk(fatigue, closer, {}, 0)

        if recent_3d.get("relievers_used", 0) > 0:
            available += 1
        if recent_7d.get("relievers_used", 0) > 0:
            available += 1
        if yesterday.get("b2b_candidates", 0) > 0:
            available += 1
        if closer.get("closer_available"):
            available += 1

        side_details[side] = {
            "recent_3d": recent_3d,
            "recent_7d": recent_7d,
            "yesterday_usage": yesterday,
            "closer": closer,
            "fatigue_score": round(fatigue, 1),
            "late_inning_risk": round(late_risk, 1),
        }

    away_fatigue = side_details.get("away", {}).get("fatigue_score", 30)
    home_fatigue = side_details.get("home", {}).get("fatigue_score", 30)
    fatigue_diff = home_fatigue - away_fatigue

    # Bullpen quality from ERA/WHIP
    savant = _dict(game.get("savant"))
    quality_scores = {}
    for side in ("away", "home"):
        pen = _dict(savant.get(f"{side}_bullpen"))
        era = _num(pen.get("ERA"), 4.25)
        whip = _num(pen.get("WHIP"), 1.35)
        score = 55 + (4.25 - era) * 5 + (1.35 - whip) * 20
        quality_scores[side] = clamp(score)
        available += sum(key in pen for key in ("ERA", "WHIP"))

    edge = clamp(55 + fatigue_diff * 0.5 + quality_scores.get("home", 50) - quality_scores.get("away", 50))

    details.update({
        "side_details": side_details,
        "quality_scores": quality_scores,
        "available_fields": available,
        "possible_fields": possible,
    })

    return edge, details


def render_bullpen_debug(slate: list[dict[str, Any]]) -> str:
    """Render bullpen debug for a slate."""
    lines = ["🧪 BULLPEN ENGINE V2 DEBUG"]
    scored = 0
    for game in slate:
        if not isinstance(game, dict):
            continue
        players = _dict(game.get("savant"))
        if not players.get("away_bullpen") and not players.get("home_bullpen"):
            continue
        edge, details = bullpen_score_v2(game)
        scored += 1
        gl = _dict(game.get("game_level"))
        matchup = gl.get("matchup") or game.get("matchup") or f"Game {game.get('game_pk', '?')}"
        lines.append(f"─ {matchup} ─")
        lines.append(f"  Edge: {edge:.1f} | quality: home {details.get('quality_scores', {}).get('home', '?')} / away {details.get('quality_scores', {}).get('away', '?')}")
        for side in ("away", "home"):
            sd = _dict(details.get("side_details", {})).get(side, {})
            lines.append(f"  {side.title()} — fatigue {sd.get('fatigue_score', '?')} / late risk {sd.get('late_inning_risk', '?')}")
            closer = _dict(sd.get("closer"))
            lines.append(f"    Closer: {closer.get('closer', '?')} (avail: {closer.get('closer_available', '?')}) / Setup: {closer.get('setup', '?')} (avail: {closer.get('setup_available', '?')})")
            r3d = _dict(sd.get("recent_3d"))
            r7d = _dict(sd.get("recent_7d"))
            yd = _dict(sd.get("yesterday_usage"))
            lines.append(f"    3d: {r3d.get('relievers_used', 0)} relievers, {r3d.get('total_pitches', 0)} pitches")
            lines.append(f"    7d: {r7d.get('relievers_used', 0)} relievers, {r7d.get('total_pitches', 0)} pitches")
            if yd.get("b2b_candidates", 0) > 0:
                lines.append(f"    B2B: {yd.get('b2b_candidates')} {yd.get('relievers')}")
    lines.append(f"Scored: {scored} games")
    return "\n".join(lines).strip()
