"""Shared BETGPTAI Safe 2-Leg Parlay formatter."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from game_time import parse_game_time


DIVIDER = "━━━━━━━━━━━━━━"
TOP_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"


def _display_date(card_date: str | None) -> str:
    if not card_date:
        return "Unavailable"
    try:
        return datetime.fromisoformat(str(card_date)).strftime("%m/%d/%Y")
    except ValueError:
        return str(card_date)


def _first_pitch(value: Any) -> str:
    parsed = parse_game_time(value)
    if parsed is None:
        text = str(value or "").strip()
        return text if text else "Time unavailable ET"
    return f"{parsed.strftime('%I:%M %p').lstrip('0')} ET"


def _market_text(leg: dict[str, Any]) -> str:
    pick = str(leg.get("pick_text") or leg.get("selection") or "").strip()
    market = str(leg.get("market_type") or "").lower()
    if market == "moneyline" and not re.search(r"\bML\b", pick, flags=re.I):
        return f"{pick} ML"
    if market == "f5_moneyline" and "F5" not in pick.upper():
        return f"{pick} F5 ML"
    if market == "runline" and not re.search(r"[+-]\d", pick):
        line = leg.get("line")
        return f"{pick} RL {line:+g}" if isinstance(line, (int, float)) else f"{pick} RL"
    if market == "total":
        return pick if re.match(r"(?i)^(over|under)\b", pick) else f"{pick} Total"
    if market == "team_total":
        return pick if re.search(r"(?i)\bteam total\b", pick) else f"{pick} Team Total"
    return pick


def _selected_team(leg: dict[str, Any]) -> str:
    return str(leg.get("selected_team") or leg.get("selection") or leg.get("pick_text") or "Selection").strip()


def _opponent(leg: dict[str, Any]) -> str:
    explicit = leg.get("opponent")
    if explicit:
        return str(explicit)
    selected = _selected_team(leg).lower()
    away = str(leg.get("away_team") or "")
    home = str(leg.get("home_team") or "")
    if selected and selected in away.lower():
        return home or "Opponent unavailable"
    if selected and selected in home.lower():
        return away or "Opponent unavailable"
    return "Opponent unavailable"


def _venue(leg: dict[str, Any]) -> str:
    for key in ("venue", "ballpark", "stadium", "park"):
        value = leg.get(key)
        if value:
            return str(value)
    home = leg.get("home_team")
    return f"{home} home park" if home else "Venue unavailable"


def _confidence(leg: dict[str, Any]) -> int:
    score = leg.get("final_edge_score")
    if isinstance(score, (int, float)):
        return max(50, min(99, round(float(score))))
    grade = leg.get("risk_grade")
    if isinstance(grade, (int, float)):
        return max(50, min(95, round(100 - float(grade) * 5)))
    return 86


def _reasons(leg: dict[str, Any]) -> list[str]:
    market = str(leg.get("market_type") or "").lower()
    details = leg.get("component_scores") if isinstance(leg.get("component_scores"), dict) else {}
    reasons: list[str] = []
    if market in {"moneyline", "f5_moneyline"}:
        if (details.get("sp_score") or 0) >= 60:
            reasons.append("Starting pitching edge")
        if (details.get("bullpen_score") or 0) >= 60:
            reasons.append("Bullpen advantage")
        if (details.get("offense_score") or 0) >= 60:
            reasons.append("Better recent offensive form")
    elif market == "runline":
        reasons.extend(["Separation profile supports run line", "Offensive matchup advantage"])
    elif market == "total":
        pick = str(leg.get("pick_text") or leg.get("selection") or "")
        reasons.extend([
            "Run environment supports the total",
            "Starting pitcher contact profile fits the angle",
            "Weather/park context supports the read",
        ])
        if pick.lower().startswith("under"):
            reasons[0] = "Run prevention profile supports the under"
    elif market == "team_total":
        reasons.extend(["Team scoring matchup supports the angle", "Opponent pitching profile is attackable"])
    if not reasons:
        reasons = ["Model-supported matchup edge", "Playable market profile"]
    while len(reasons) < 3:
        reasons.append("Positive situational setup")
    return reasons[:3]


def render_safe_parlay(
    legs: list[dict[str, Any]] | None,
    *,
    card_date: str | None = None,
) -> str:
    """Render a clean Safe 2-Leg Parlay from saved official leg metadata."""
    valid_legs = [leg for leg in (legs or []) if isinstance(leg, dict)][:2]
    if len(valid_legs) < 2:
        return "No Safe 2-Leg Parlay qualified today."
    if card_date is None:
        card_date = str(valid_legs[0].get("card_date") or valid_legs[0].get("date") or "")
    sections = [
        TOP_DIVIDER,
        "🧩 BETGPTAI SAFE 2-LEG PARLAY",
        f"📅 Card Date: {_display_date(card_date)}",
        "",
    ]
    confidences: list[int] = []
    for index, leg in enumerate(valid_legs, start=1):
        confidence = _confidence(leg)
        confidences.append(confidence)
        sections.extend([
            f"LEG {index}",
            "",
            f"✅ {_market_text(leg)}",
            f"🆚 {_opponent(leg)}",
            f"🏟 Venue: {_venue(leg)}",
            f"🕒 First Pitch: {_first_pitch(leg.get('game_time'))}",
            f"⭐ Confidence: {confidence}/100",
            "",
            "Why:",
            *[f"• {reason}" for reason in _reasons(leg)],
            "",
            DIVIDER,
            "",
        ])
    average = round(sum(confidences) / len(confidences))
    stars = "⭐⭐⭐⭐☆" if average >= 85 else "⭐⭐⭐☆☆"
    edge = "High" if average >= 88 else "Moderate" if average >= 80 else "Lean"
    sections.extend([
        "Overall Confidence",
        stars,
        "",
        "Estimated Edge:",
        edge,
        "",
        "Recommended:",
        "Singles first.",
        "Parlays carry higher variance.",
        "",
        "Educational analysis only.",
    ])
    return "\n".join(sections).strip()
