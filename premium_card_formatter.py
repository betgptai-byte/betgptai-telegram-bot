"""BETGPTAI Premium Card UI v3.0 renderers.

This module changes presentation only. It reads saved pick/prop/admin data and
renders it as consistent sportsbook-analyst terminal blocks.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from game_time import parse_game_time
from safe_parlay_formatter import render_safe_parlay
from storage import data_file


DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━"
FOOTER = "Educational analysis only.\n\nSingles are recommended.\n\nParlays carry greater variance."


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _display_date(card_date: str) -> str:
    try:
        return datetime.fromisoformat(card_date).strftime("%m/%d/%Y")
    except Exception:
        return str(card_date or "Unavailable")


def _time_label(value: Any, status: Any = None) -> str:
    status_text = str(status or "").lower()
    if any(word in status_text for word in ("final", "game over", "completed")):
        return "✅ FINAL"
    if any(word in status_text for word in ("live", "in progress", "warmup", "started")):
        return "🔴 LIVE"
    parsed = parse_game_time(value)
    if parsed is None:
        text = str(value or "").strip()
        if text.lower() in {"started", "live"}:
            return "🔴 LIVE"
        return text if text and text != "Unknown" else "Time unavailable ET"
    return f"{parsed.strftime('%I:%M %p').lstrip('0')} ET"


def _market_label(pick: dict[str, Any]) -> str:
    market = str(pick.get("market_type") or pick.get("pick_type") or "").lower()
    if market == "moneyline":
        return "Moneyline"
    if market == "f5_moneyline":
        return "F5 Moneyline"
    if market == "runline":
        line = pick.get("line")
        return f"Runline {line:+g}" if isinstance(line, (int, float)) else "Runline"
    if market == "total":
        return "Game Total"
    if market == "team_total":
        return "Team Total"
    if market == "parlay":
        return "Safe 2-Leg Parlay"
    return market.replace("_", " ").title() or "Market"


def _pick_name(pick: dict[str, Any]) -> str:
    text = str(pick.get("pick_text") or pick.get("selection") or "Pick unavailable").strip()
    market = str(pick.get("market_type") or "").lower()
    if market == "team_total":
        match = re.search(r"(?i)\b(over|under)\b", text)
        return f"Team Total {match.group(1).upper()}" if match else "Team Total"
    if market == "total":
        match = re.search(r"(?i)\b(over|under)\s+(\d+(?:\.\d+)?)", text)
        return f"{match.group(1).upper()} {match.group(2)}" if match else text
    if market == "moneyline" and not re.search(r"\bML\b", text, re.I):
        return f"{text} ML"
    if market == "f5_moneyline" and "F5" not in text.upper():
        return f"{text} F5 ML"
    if market == "runline" and not re.search(r"[+-]\d", text):
        line = pick.get("line")
        return f"{text} {line:+g}" if isinstance(line, (int, float)) else text
    return text


def _team(pick: dict[str, Any]) -> str:
    return str(pick.get("selected_team") or pick.get("team_name") or pick.get("team") or "N/A")


def _opponent(pick: dict[str, Any]) -> str:
    explicit = pick.get("opponent") or pick.get("opponent_name")
    if explicit:
        return str(explicit)
    selected = _team(pick).lower()
    away = str(pick.get("away_team") or "")
    home = str(pick.get("home_team") or "")
    if selected and selected in away.lower():
        return home or "Opponent unavailable"
    if selected and selected in home.lower():
        return away or "Opponent unavailable"
    return "Opponent unavailable"


def _venue(pick: dict[str, Any]) -> str:
    for key in ("venue", "ballpark", "stadium", "park"):
        if pick.get(key):
            return str(pick[key])
    home = pick.get("home_team")
    return f"{home} home park" if home else "Venue unavailable"


def _edge_score(pick: dict[str, Any]) -> int:
    value = pick.get("final_edge_score") or pick.get("edge_score") or pick.get("raw_score")
    if isinstance(value, (int, float)):
        return max(1, min(100, round(float(value))))
    grade = pick.get("risk_grade")
    if isinstance(grade, (int, float)):
        return max(50, min(95, round(100 - float(grade) * 5)))
    return 86


def _confidence(score: int) -> str:
    if score >= 92:
        return "Elite"
    if score >= 86:
        return "Strong"
    if score >= 78:
        return "Playable"
    return "Lean"


def _risk(score: int, pick: dict[str, Any]) -> str:
    if pick.get("risk_level"):
        return str(pick["risk_level"])
    if score >= 88:
        return "Low"
    if score >= 78:
        return "Medium"
    return "High"


def _reasons(pick: dict[str, Any]) -> list[str]:
    details = pick.get("component_scores") if isinstance(pick.get("component_scores"), dict) else {}
    market = str(pick.get("market_type") or "").lower()
    reasons: list[str] = []
    if market == "f5_moneyline":
        reasons.append("Starting pitching edge")
    elif market == "runline":
        reasons.append("Separation profile")
    elif market == "total":
        reasons.append("Run environment")
    elif market == "team_total":
        reasons.append("Team scoring matchup")
    else:
        reasons.append("Starting pitching")
    if (details.get("bullpen_score") or 0) >= 55:
        reasons.append("Bullpen")
    if (details.get("offense_score") or 0) >= 55:
        reasons.append("Offensive split")
    if (details.get("weather_park_score") or 0) >= 55:
        reasons.append("Weather/park")
    for fallback in ("Market profile", "Situational setup", "Model-supported edge"):
        if len(reasons) >= 3:
            break
        if fallback not in reasons:
            reasons.append(fallback)
    return reasons[:3]


def render_pick_block(
    pick: dict[str, Any],
    *,
    rank: int | None = None,
    show_data_quality: bool = False,
) -> str:
    """Render one pick in the v3.0 premium block format."""
    score = _edge_score(pick)
    prefix = f"#{rank}\n\n" if rank else ""
    data_quality = f"\nData Quality:\n{pick.get('data_quality_grade') or 'N/A'}\n" if show_data_quality else ""
    market = str(pick.get("market_type") or "").lower()
    extra = ""
    if market == "team_total":
        text = str(pick.get("pick_text") or pick.get("selection") or "")
        line_match = re.search(r"(?i)\b(over|under)\s+(\d+(?:\.\d+)?)", text)
        line_text = f"{line_match.group(1).title()} {line_match.group(2)}" if line_match else "Line unavailable"
        safer = ""
        safer_match = re.search(r"(?i)Safer Alt:?\s*(Over|Under)\s+(\d+(?:\.\d+)?)", text)
        if safer_match:
            safer = f"\nSafer Alt\n\n{safer_match.group(1).title()} {safer_match.group(2)}\n\n"
        extra = f"Line\n\n{line_text}\n\n{safer}"
    elif market == "total":
        line = pick.get("line")
        projected = f"{float(line):g}" if isinstance(line, (int, float)) else "Unavailable"
        extra = f"Projected Total\n\n{projected}\n\n"
    return (
        f"{DIVIDER}\n\n"
        f"{prefix}"
        f"✅ {_pick_name(pick)}\n\n"
        f"Market:\n{_market_label(pick)}\n\n"
        f"Team:\n{_team(pick)}\n\n"
        f"Opponent:\n{_opponent(pick)}\n\n"
        f"🏟 Venue:\n{_venue(pick)}\n\n"
        f"🕒 First Pitch:\n{_time_label(pick.get('game_time'), pick.get('game_status') or pick.get('status'))}\n\n"
        f"{extra}"
        f"⭐ Edge Score:\n{score}/100\n\n"
        f"🔥 Confidence:\n{_confidence(score)}\n"
        f"{data_quality}\n"
        "BETGPTAI EDGE\n\n"
        + "\n\n".join(f"• {reason}" for reason in _reasons(pick))
        + "\n\nRisk\n\n"
        f"{_risk(score, pick)}\n\n"
        f"{DIVIDER}"
    )


def _load_picks_for_date(card_date: str) -> list[dict[str, Any]]:
    picks = _read_json(data_file("picks.json"), [])
    if not isinstance(picks, list):
        return []
    return [
        pick for pick in picks
        if isinstance(pick, dict)
        and str(pick.get("card_date") or pick.get("date") or "") == card_date
        and pick.get("category") != "parlay_leg"
    ]


def _latest_by_category(picks: list[dict[str, Any]], category: str, limit: int = 5) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for pick in reversed(picks):
        if pick.get("category") != category and pick.get("market_type") != category:
            continue
        key = str(pick.get("pick_id") or pick.get("pick_text"))
        if key in seen:
            continue
        seen.add(key)
        output.append(pick)
        if len(output) >= limit:
            break
    return list(reversed(output))


def render_mlb_premium_card(card_date: str, *, include_footer: bool = True) -> str:
    """Render today's saved MLB card with premium v3.0 block UI."""
    picks = _load_picks_for_date(card_date)
    if not picks:
        return "No official MLB picks saved for this card date yet."
    play = next((pick for pick in reversed(picks) if pick.get("category") == "play_of_day"), None)
    parlay = next((pick for pick in reversed(picks) if pick.get("category") == "parlay"), None)
    sections: list[str] = [
        "⚾ BETGPTAI PREMIUM MLB CARD",
        f"📅 Card Date: {_display_date(card_date)}",
    ]
    if play:
        sections.extend(["", "🔥 PLAY OF THE DAY", "", render_pick_block(play)])
    for title, category in (
        ("🏆 TOP MONEYLINES", "moneyline"),
        ("📈 TOP RUNLINES", "runline"),
        ("🔥 TOP F5 MONEYLINE", "f5_moneyline"),
        ("🎯 TOP GAME TOTALS", "total"),
        ("💰 TOP TEAM TOTALS", "team_total"),
    ):
        rows = _latest_by_category(picks, category, 5)
        if not rows:
            continue
        sections.extend(["", title, ""])
        sections.extend(render_pick_block(row, rank=index) for index, row in enumerate(rows, start=1))
    if isinstance(parlay, dict):
        legs = parlay.get("legs") if isinstance(parlay.get("legs"), list) else []
        sections.extend(["", render_safe_parlay(legs, card_date=card_date)])
    if include_footer:
        sections.extend(["", FOOTER])
    return "\n\n".join(sections).strip()


def render_play_of_day_card(card_date: str) -> str:
    picks = _load_picks_for_date(card_date)
    play = next((pick for pick in reversed(picks) if pick.get("category") == "play_of_day"), None)
    if not play:
        return "Today’s Play of the Day is still being prepared."
    return (
        "🔥 BETGPTAI PLAY OF THE DAY\n"
        f"📅 Card Date: {_display_date(card_date)}\n\n"
        f"{render_pick_block(play)}\n\n{FOOTER}"
    )


def render_category_card(card_date: str, title: str, category: str, *, admin: bool = False) -> str:
    picks = _load_picks_for_date(card_date)
    rows = _latest_by_category(picks, category, 5)
    if not rows:
        return f"{title}\n\nNo qualified plays available."
    sections = [
        title,
        f"📅 Card Date: {_display_date(card_date)}",
        "🧪 Admin Only" if admin else "",
        "",
    ]
    sections.extend(render_pick_block(row, rank=index, show_data_quality=admin) for index, row in enumerate(rows, start=1))
    if not admin:
        sections.extend(["", FOOTER])
    return "\n\n".join(section for section in sections if section != "").strip()


def render_prop_block(prop: dict[str, Any], *, label: str = "PROP", rank: int | None = None) -> str:
    score = _edge_score(prop)
    prefix = f"#{rank}\n\n" if rank else ""
    return (
        f"{DIVIDER}\n\n"
        f"{prefix}"
        f"✅ {label}\n\n"
        "Market:\nProp\n\n"
        f"Team:\n{prop.get('team_name') or prop.get('team') or 'N/A'}\n\n"
        f"Opponent:\n{prop.get('opponent_name') or prop.get('opponent') or 'Opponent unavailable'}\n\n"
        f"🏟 Venue:\n{prop.get('venue') or prop.get('ballpark') or prop.get('game_matchup') or 'Venue unavailable'}\n\n"
        f"🕒 First Pitch:\n{prop.get('game_time_et') or _time_label(prop.get('game_time'))}\n\n"
        f"⭐ Edge Score:\n{score}/100\n\n"
        f"🔥 Confidence:\n{prop.get('confidence_grade') or _confidence(score)}\n\n"
        "BETGPTAI EDGE\n\n"
        f"• {prop.get('reason') or 'Verified prop context'}\n\n"
        "Risk\n\n"
        f"{_risk(score, prop)}\n\n"
        f"{DIVIDER}"
    )
