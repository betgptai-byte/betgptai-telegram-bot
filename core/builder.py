"""Build a StructuredCard from AI analysis + slate data.

No Telegram-formatted-text parsing.  Uses section headings (the structural
contract between prompt and builder) to identify picks, then enriches each
pick with game data, quant scores, and odds from the slate.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from core.card import StructuredCard
from core.pick import OfficialPick


SECTION_HEADINGS: dict[str, str] = {
    "play_of_day": "🔥 PLAY OF THE DAY",
    "moneyline": "🏆 TOP 5 MONEYLINE",
    "f5": "🔥 TOP 5 F5",
    "runline": "📈 TOP 5 RUN LINE",
    "totals": "🎯 TOP 5 GAME TOTALS",
    "team_totals": "💰 TOP 5 TEAM TOTALS",
    "parlay": "🧩 2-LEG SAFE PARLAY",
}

HEADING_SET: set[str] = set(SECTION_HEADINGS.values())

SECTION_ORDER: list[str] = [
    "play_of_day", "moneyline", "f5", "runline", "totals", "team_totals", "parlay",
]

# ── Stats-only fallback section list (broader heading set than the prompt) ──
_STATS_SECTION_HEADINGS: list[tuple[str, str]] = [
    ("play_of_day", "🔥 PLAY OF THE DAY"),
    ("moneyline", "🏆 TOP 2 MONEYLINE"),
    ("moneyline", "🏆 TOP 5 MONEYLINE"),
    ("f5", "🔥 TOP 2 F5 MONEYLINE"),
    ("f5", "🔥 TOP 5 F5"),
    ("f5", "🔥 TOP 5 F5 MONEYLINE"),
    ("f5", "🔥 F5 MONEYLINE LEAN"),
    ("runline", "📈 TOP 2 RUNLINE/SPREAD"),
    ("runline", "📈 TOP 5 RUNLINE/SPREAD"),
    ("runline", "📈 TOP 5 RUN LINE"),
    ("totals", "🎯 TOP 2 OVER/UNDER TOTAL RUNS"),
    ("totals", "🎯 TOP 5 OVER/UNDER TOTAL RUNS"),
    ("totals", "🎯 TOP 5 GAME TOTALS"),
    ("team_totals", "💰 TOP 2 TEAM TOTALS"),
    ("team_totals", "💰 TEAM TOTAL ANGLE"),
    ("team_totals", "💰 TOP 5 TEAM TOTALS"),
    ("parlay", "🧩 2-LEG SAFE PARLAY"),
    ("parlay", "🧩 SAFE PARLAY OF THE DAY"),
]


def _stats_only_mode() -> bool:
    return os.getenv("STATS_ONLY_CARD_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def _heading_text(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 /-]", "", value).strip()


def _find_headings(analysis: str) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    lines = analysis.splitlines()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        h = _heading_text(stripped)
        for key, heading in SECTION_HEADINGS.items():
            if _heading_text(heading) == h and stripped not in ("", "None"):
                found.append((key, idx))
                break
    found.sort(key=lambda x: x[1])
    return found


def _section_content(analysis: str, start_idx: int, end_idx: int | None) -> str:
    lines = analysis.splitlines()
    chunk = lines[start_idx + 1:end_idx] if end_idx else lines[start_idx + 1:]
    return "\n".join(chunk).strip()


def _normalize_team(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    aliases = {
        "arizonadiamondbacks": "diamondbacks", "arizonadbacks": "diamondbacks",
        "atlantabraves": "braves", "baltimoreorioles": "orioles",
        "bostonredsox": "redsox", "chicagocubs": "cubs",
        "chicagowhitesox": "whitesox", "cincinnatireds": "reds",
        "clevelandguardians": "guardians", "coloradorockies": "rockies",
        "detroittigers": "tigers", "houstonastros": "astros",
        "kansascityroyals": "royals", "losangelesangels": "angels",
        "laangels": "angels", "losangelesdodgers": "dodgers",
        "ladodgers": "dodgers", "miamimarlins": "marlins",
        "milwaukeebrewers": "brewers", "minnesotatwins": "twins",
        "newyorkmets": "mets", "nymets": "mets",
        "newyorkyankees": "yankees", "nyyankees": "yankees",
        "oaklandathletics": "athletics", "sacramentoathletics": "athletics",
        "athletics": "athletics", "philadelphiaphillies": "phillies",
        "pittsburghpirates": "pirates", "sandiegopadres": "padres",
        "sanfranciscogiants": "giants", "seattlemariners": "mariners",
        "stlouiscardinals": "cardinals", "saintlouiscardinals": "cardinals",
        "tampabayrays": "rays", "texasrangers": "rangers",
        "torontobluejays": "bluejays", "washingtonnationals": "nationals",
    }
    return aliases.get(normalized, normalized)


def _game_for_selection(selection: str, slate: list[dict[str, Any]]) -> dict[str, Any] | None:
    norm_sel = _normalize_team(selection)
    matches: list[dict[str, Any]] = []
    for game in slate:
        tokens: list[str] = []
        for key in ("away_team", "home_team"):
            team = str(game.get(key, ""))
            full = _normalize_team(team)
            nickname = _normalize_team(team.split()[-1] if team.split() else "")
            tokens.extend(t for t in (full, nickname) if len(t) >= 3)
        if any(token in norm_sel for token in tokens):
            matches.append(game)
    if len(matches) == 1:
        return matches[0]
    unique = {game.get("game_id"): game for game in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _extract_team_from_selection(selection: str) -> str | None:
    stripped = re.sub(r"\s+ML\b", "", selection, flags=re.I).strip()
    stripped = re.sub(r"\s+F5\b", "", stripped, flags=re.I).strip()
    stripped = re.sub(r"\s+[+-]\d+(?:\.\d+)?", "", stripped).strip()
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", stripped).strip()
    stripped = re.sub(r"^✅\s*", "", stripped).strip()
    parts = stripped.split()
    for length in range(len(parts), 0, -1):
        candidate = " ".join(parts[:length])
        if len(candidate) >= 3:
            return candidate
    return stripped if len(stripped) >= 3 else None


def _parse_pick_lines(content: str) -> list[str]:
    picks: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = re.sub(r"^[⚾1️⃣2️⃣3️⃣4️⃣5️⃣✅*_`\d.)\s]+", "", stripped).strip()
        if not cleaned:
            continue
        if cleaned.startswith(("Risk", "Line", "Safer", "No", "None", "Unavailable", "🆚")):
            continue
        if len(cleaned) < 5:
            continue
        picks.append(cleaned)
    return picks


def _extract_selection_team(selection: str, game: dict[str, Any]) -> str | None:
    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))
    if _normalize_team(away) in _normalize_team(selection) or away.lower() in selection.lower():
        return away
    if _normalize_team(home) in _normalize_team(selection) or home.lower() in selection.lower():
        return home
    return None


def _pick_id_from_parts(parts: list[str]) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _american_odds_from_slate(game: dict[str, Any], market_key: str, outcome: str | None) -> int | float | None:
    for wager in game.get("best_available_prices", []):
        if wager.get("market") != market_key:
            continue
        if outcome and not _normalize_team(str(wager.get("outcome", ""))) in _normalize_team(outcome):
            continue
        price = wager.get("price")
        if price is not None:
            return int(price) if isinstance(price, float) and price.is_integer() else price
    return None


def _american_odds(value: str | int | float | None) -> int | float | None:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"[+-]?\d+(?:\.\d+)?", value)
    if not match:
        return None
    number = float(match.group())
    return int(number) if number.is_integer() else number


def _parse_line_odds(selection: str) -> int | float | None:
    match = re.search(r"\(([+-]\d+)\)", selection)
    if match:
        return int(match.group(1))
    return None


def _market_type_for_section(section_key: str) -> str:
    mapping = {
        "play_of_day": "moneyline",
        "moneyline": "moneyline",
        "f5": "f5_moneyline",
        "runline": "runline",
        "totals": "total",
        "team_totals": "team_total",
        "parlay": "parlay",
    }
    return mapping.get(section_key, "moneyline")


def _market_key_for_type(market_type: str) -> str:
    mapping = {
        "moneyline": "h2h",
        "f5_moneyline": "f5_h2h",
        "runline": "spreads",
        "total": "totals",
        "team_total": "team_totals",
    }
    return mapping.get(market_type, "h2h")


def _game_time_et(game: dict[str, Any]) -> str | None:
    raw = game.get("game_time") or game.get("game_time_et")
    if not raw:
        return None
    raw_str = str(raw)
    try:
        from datetime import datetime
        for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%I:%M %p"):
            try:
                parsed = datetime.strptime(raw_str, pattern)
                return f"{parsed.strftime('%I:%M %p').lstrip('0')} ET"
            except ValueError:
                continue
    except Exception:
        pass
    if "ET" in raw_str:
        return raw_str
    return raw_str


def _quant_payload(game: dict[str, Any]) -> dict[str, Any]:
    quant = game.get("betgptai_quant_v20") or game.get("betgptai_internal") or {}
    if isinstance(quant, dict) and isinstance(quant.get("v20"), dict):
        quant = quant["v20"]
    return quant if isinstance(quant, dict) else {}


def _build_official_pick(
    selection: str,
    section_key: str,
    game: dict[str, Any],
    card_date: str,
) -> OfficialPick | None:
    market_type = _market_type_for_section(section_key)
    if market_type in ("total",):
        selected_team = None
        opponent = None
    else:
        selected_team = _extract_selection_team(selection, game)
        if not selected_team and market_type in ("moneyline", "runline", "f5_moneyline", "team_total"):
            extracted = _extract_team_from_selection(selection)
            if extracted:
                selected_team = extracted

    if not selected_team and market_type in ("moneyline", "runline", "f5_moneyline"):
        return None
    if market_type == "team_total" and not selected_team:
        return None

    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))
    if market_type in ("total",):
        opponent = None
    else:
        opponent = home if selected_team and _normalize_team(selected_team) == _normalize_team(away) else away

    line_value: float | None = None
    odds_value: int | float | None = _parse_line_odds(selection)

    market_key = _market_key_for_type(market_type)
    mk = _american_odds_from_slate(game, market_key, selected_team)
    if mk is not None:
        odds_value = mk

    if market_type in ("runline",):
        ml = re.search(r"[+-]\d+(?:\.\d+)?", selection)
        if ml:
            line_value = float(ml.group())
    elif market_type in ("total",):
        ml = re.search(r"(?:Over|Under)\s+(\d+(?:\.\d+)?)", selection, flags=re.I)
        if ml:
            line_value = float(ml.group(1))
    elif market_type in ("team_total",):
        ml = re.search(r"(?:Over|Under)\s+(\d+(?:\.\d+)?)", selection, flags=re.I)
        if ml:
            line_value = float(ml.group(1))

    quant = _quant_payload(game)
    edge = quant.get("final_edge_score")
    confidence = quant.get("confidence")
    risk = quant.get("risk_level")
    dq = quant.get("data_quality_grade")
    model_version = quant.get("model_version", "BETGPTAI v20.0")

    game_pk = game.get("game_pk") or game.get("game_id")
    if isinstance(game_pk, list):
        game_pk = game_pk[0] if game_pk else None

    pick_id = _pick_id_from_parts([
        card_date, str(game_pk), market_type,
        selected_team or "", str(line_value or ""),
    ])

    return OfficialPick(
        pick_id=pick_id,
        sport="mlb",
        league="MLB",
        card_date=card_date,
        game_pk=int(game_pk) if game_pk is not None else None,
        game_time_et=_game_time_et(game),
        away_team=away,
        home_team=home,
        selected_team=selected_team,
        opponent=opponent if opponent != (selected_team or "") else None,
        market_type=market_type,
        market_line=line_value,
        odds=odds_value,
        confidence=confidence,
        edge_score=float(edge) if edge is not None else None,
        risk_level=risk,
        data_quality_grade=dq,
        units=1.0,
        reason="",
        status="pending",
        result=None,
        model_version=model_version or "BETGPTAI v20.0",
    )


def _parlay_legs(
    content: str,
    slate: list[dict[str, Any]],
    card_date: str,
) -> list[OfficialPick]:
    legs: list[OfficialPick] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("✅"):
            continue
        selection = re.sub(r"^✅\s*", "", stripped).strip()
        if not selection or len(selection) < 5:
            continue
        game = _game_for_selection(selection, slate)
        if not game:
            continue
        pick = _build_official_pick(selection, "moneyline", game, card_date)
        if pick:
            legs.append(pick)
    return legs[:2]


# ── Stats-only builder (no odds required) ───────────────────────────────────

_STATS_TEAM_ALIASES: dict[str, str] = {
    "diamondbacks": "arizonadiamondbacks", "dbacks": "arizonadiamondbacks",
    "braves": "atlantabraves", "orioles": "baltimoreorioles",
    "redsox": "bostonredsox", "cubs": "chicagocubs",
    "whitesox": "chicagowhitesox", "reds": "cincinnatireds",
    "guardians": "clevelandguardians", "rockies": "coloradorockies",
    "tigers": "detroittigers", "astros": "houstonastros",
    "royals": "kansascityroyals", "angels": "losangelesangels",
    "dodgers": "losangelesdodgers", "marlins": "miamimarlins",
    "brewers": "milwaukeebrewers", "twins": "minnesotatwins",
    "mets": "newyorkmets", "yankees": "newyorkyankees",
    "athletics": "oaklandathletics", "phillies": "philadelphiaphillies",
    "pirates": "pittsburghpirates", "padres": "sandiegopadres",
    "giants": "sanfranciscogiants", "mariners": "seattlemariners",
    "cardinals": "stlouiscardinals", "rays": "tampabayrays",
    "rangers": "texasrangers", "bluejays": "torontobluejays",
    "nationals": "washingtonnationals",
}


def _stats_normalize(name: str) -> str:
    """Lowercase, strip non-alphanumeric, and resolve common nickname/abbrev."""
    raw = re.sub(r"[^a-z0-9]", "", name.lower())
    return _STATS_TEAM_ALIASES.get(raw, raw)


def _stats_match_game(selection: str, slate: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a pick line to a slate game.  Tries full name, nickname, common abbrevs."""
    sel_normalized = _stats_normalize(selection)
    matches: list[dict[str, Any]] = []
    for game in slate:
        for key in ("away_team", "home_team"):
            raw = str(game.get(key, ""))
            full = _stats_normalize(raw)
            nick = _stats_normalize(raw.split()[-1] if raw.split() else "")
            if (full and full in sel_normalized) or (nick and len(nick) >= 2 and nick in sel_normalized):
                matches.append(game)
                break
    if len(matches) == 1:
        return matches[0]
    unique = {g.get("game_pk") or g.get("game_id"): g for g in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _stats_extract_team(selection: str, game: dict[str, Any]) -> str | None:
    """Return which side (away/home) the selection refers to."""
    for key in ("away_team", "home_team"):
        raw = str(game.get(key, ""))
        norm = _stats_normalize(raw)
        if norm and norm in _stats_normalize(selection):
            return raw
    return None


def _stats_parse_line_value(selection: str, market_type: str) -> float | None:
    """Extract line value from pick text (totals, team-totals, runlines)."""
    if market_type in ("total", "team_total"):
        m = re.search(r"(?:Over|Under)\s+(\d+(?:\.\d+)?)", selection, flags=re.I)
        return float(m.group(1)) if m else None
    if market_type == "runline":
        m = re.search(r"[+-]\d+(?:\.\d+)?", selection)
        return float(m.group()) if m else None
    return None


def _stats_market_type(section_key: str) -> str:
    mapping = {
        "play_of_day": "moneyline",
        "moneyline": "moneyline",
        "f5": "f5_moneyline",
        "runline": "runline",
        "totals": "total",
        "team_totals": "team_total",
        "parlay": "parlay",
    }
    return mapping.get(section_key, "moneyline")


def _stats_section_content(analysis: str, heading: str) -> str:
    """Return content between *heading* and the next heading or end."""
    start = analysis.find(heading)
    if start < 0:
        return ""
    body_start = start + len(heading)
    end = len(analysis)
    for marker in ("\n---", "\n━━━", "\n🔥", "\n🏆", "\n📈", "\n🎯", "\n💰", "\n🧩"):
        pos = analysis.find(marker, body_start + 1)
        if pos >= 0:
            end = min(end, pos)
    return analysis[body_start:end].strip() if end > body_start else ""


def _stats_pick_lines(section_body: str) -> list[str]:
    """Extract individual pick lines from a section body (no odds required)."""
    lines: list[str] = []
    for line in section_body.splitlines():
        s = line.strip()
        if not s:
            continue
        cleaned = re.sub(r"^[⚾1️⃣2️⃣3️⃣4️⃣5️⃣✅*_`\d.)\s]+", "", s).strip()
        if not cleaned:
            continue
        if cleaned.startswith(("Risk", "Line", "Safer", "No", "None", "Unavailable", "🆚")):
            continue
        if len(cleaned) < 5:
            continue
        lines.append(cleaned)
    return lines


def _build_stats_only_official_picks(
    analysis: str,
    slate: list[dict[str, Any]],
    card_date: str,
) -> dict[str, Any]:
    """Build OfficialPick objects from analysis sections in stats-only mode.

    Returns a dict with keys:
      picks: list[OfficialPick]
      sections_found: list[str]
      section_item_counts: dict[str, int]
      rejected_items: list[str]
      rejection_reasons: list[str]
    """
    from datetime import datetime, timezone

    result_picks: list[OfficialPick] = []
    sections_found: list[str] = []
    section_item_counts: dict[str, int] = {}
    rejected_items: list[str] = []
    rejection_reasons: list[str] = []

    seen_hashes: set[str] = set()

    for section_key, heading in _STATS_SECTION_HEADINGS:
        if heading not in analysis:
            continue
        content = _stats_section_content(analysis, heading)
        if not content:
            continue
        sections_found.append(heading)
        market_type = _stats_market_type(section_key)

        if section_key == "parlay":
            leg_lines = re.findall(r"(?m)^✅\s+(.+)$", content)[:2]
            section_item_counts[heading] = len(leg_lines)
            for leg_text in leg_lines:
                game = _stats_match_game(leg_text, slate)
                if not game:
                    rejected_items.append(leg_text)
                    rejection_reasons.append(f"Parlay leg no game match: {leg_text[:60]}")
                    continue
                pick = _build_stats_official_pick(leg_text, "moneyline", game, card_date)
                if pick:
                    if pick.pick_id not in seen_hashes:
                        seen_hashes.add(pick.pick_id)
                        result_picks.append(pick)
                else:
                    rejected_items.append(leg_text)
                    rejection_reasons.append(f"Parlay leg build failed: {leg_text[:60]}")
            continue

        pick_lines = _stats_pick_lines(content)
        section_item_counts[heading] = len(pick_lines)

        for line_text in pick_lines:
            game = _stats_match_game(line_text, slate)
            if not game:
                rejected_items.append(line_text)
                rejection_reasons.append(f"No game match ({market_type}): {line_text[:60]}")
                continue

            line_value = _stats_parse_line_value(line_text, market_type)

            # Totals/team-totals require a verified line to be saved
            if market_type in ("total", "team_total") and line_value is None:
                rejected_items.append(line_text)
                rejection_reasons.append(f"No line value for {market_type}: {line_text[:60]}")
                continue

            # Runline requires a valid spread line
            if market_type == "runline" and line_value is None:
                rejected_items.append(line_text)
                rejection_reasons.append(f"No runline spread value: {line_text[:60]}")
                continue

            pick = _build_stats_official_pick(line_text, market_type, game, card_date, line_value=line_value)
            if pick:
                if pick.pick_id not in seen_hashes:
                    seen_hashes.add(pick.pick_id)
                    result_picks.append(pick)
            else:
                rejected_items.append(line_text)
                rejection_reasons.append(f"Pick build failed ({market_type}): {line_text[:60]}")

    return {
        "picks": result_picks,
        "sections_found": sections_found,
        "section_item_counts": section_item_counts,
        "rejected_items": rejected_items,
        "rejection_reasons": rejection_reasons,
    }


def _build_stats_official_pick(
    selection: str,
    market_type: str,
    game: dict[str, Any],
    card_date: str,
    line_value: float | None = None,
) -> OfficialPick | None:
    """Build a single stats-only OfficialPick (no odds required)."""
    selected_team: str | None = None
    opponent: str | None = None
    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))

    if market_type not in ("total", "parlay"):
        selected_team = _stats_extract_team(selection, game)
        if not selected_team:
            # Fallback: try full nickname match from normalized text
            sel_norm = _stats_normalize(selection)
            for t in (away, home):
                if _stats_normalize(t) and _stats_normalize(t) in sel_norm:
                    selected_team = t
                    break
        if selected_team:
            opponent = home if _stats_normalize(selected_team) == _stats_normalize(away) else away

    # Still no team match for side markets → drop
    if not selected_team and market_type in ("moneyline", "runline", "f5_moneyline"):
        return None

    game_pk = game.get("game_pk") or game.get("game_id")
    if isinstance(game_pk, list):
        game_pk = game_pk[0] if game_pk else None

    quant = game.get("betgptai_quant_v20") or game.get("betgptai_internal") or {}
    if isinstance(quant, dict) and isinstance(quant.get("v20"), dict):
        quant = quant["v20"]

    pick_id = _pick_id_from_parts([card_date, str(game_pk), market_type, selected_team or "", str(line_value or "")])

    from datetime import datetime, timezone

    return OfficialPick(
        pick_id=pick_id,
        sport="mlb",
        league="MLB",
        card_date=card_date,
        game_pk=int(game_pk) if game_pk is not None else None,
        game_time_et=_game_time_et(game),
        away_team=away,
        home_team=home,
        selected_team=selected_team,
        opponent=opponent,
        market_type=market_type,
        market_line=line_value,
        odds=None,
        confidence=quant.get("confidence"),
        edge_score=float(quant["final_edge_score"]) if quant.get("final_edge_score") is not None else None,
        risk_level=quant.get("risk_level"),
        data_quality_grade=quant.get("data_quality_grade"),
        units=1.0,
        reason="",
        status="pending",
        result=None,
        model_version=quant.get("model_version", "BETGPTAI v20.0"),
        # Stats-only metadata
        market_mode="stats_only",
        odds_status="unavailable",
        market_context_status="stats_only",
        sportsbook="none",
        posted_line=None,
        line_verified=False,
        official_pick_source="stats_only_builder",
    )


def build_card_from_analysis(
    analysis: str,
    slate: list[dict[str, Any]],
    card_date: str,
    source_command: str = "unknown",
) -> StructuredCard:
    all_picks: list[OfficialPick] = []
    display_sections: dict[str, list[str]] = {}
    errors: list[str] = []
    stats_only_builder_picks_created = 0
    stats_sections_found: list[str] = []
    stats_section_item_counts: dict[str, int] = {}

    if _stats_only_mode() and slate:
        # Stats-only mode: build picks from sections directly (no odds required)
        stats_result = _build_stats_only_official_picks(analysis, slate, card_date)
        all_picks = stats_result["picks"]
        stats_sections_found = stats_result.get("sections_found", [])
        stats_section_item_counts = stats_result.get("section_item_counts", {})
        rejected = stats_result.get("rejected_items", [])
        reasons = stats_result.get("rejection_reasons", [])
        if reasons:
            errors.extend(reasons[:20])
        if all_picks:
            stats_only_builder_picks_created = len(all_picks)
            for pk in all_picks:
                mt = pk.market_type
                label = {"moneyline": "Moneyline", "f5_moneyline": "F5", "runline": "Runline",
                         "total": "Total", "team_total": "Team Total", "parlay": "Parlay"}.get(mt, mt)
                display_sections.setdefault(label, []).append(str(pk.selected_team or ""))
        elif stats_sections_found:
            stats_only_builder_picks_created = 0
    else:
        headings = _find_headings(analysis)
        for idx, (section_key, start_idx) in enumerate(headings):
            end_idx = headings[idx + 1][1] if idx + 1 < len(headings) else None
            content = _section_content(analysis, start_idx, end_idx)
            if not content:
                continue

            if section_key == "parlay":
                legs = _parlay_legs(content, slate, card_date)
                _display_key = "Parlay"
                display_sections[_display_key] = [str(p.selected_team or p.market_type) for p in legs]
                all_picks.extend(legs)
                continue

            _section_picks: list[OfficialPick] = []
            pick_lines = _parse_pick_lines(content)
            for line in pick_lines:
                game = _game_for_selection(line, slate)
                if not game:
                    errors.append(f"No game match: {line[:60]}")
                    continue
                pick = _build_official_pick(line, section_key, game, card_date)
                if pick:
                    _section_picks.append(pick)
                else:
                    errors.append(f"Could not build pick: {line[:60]}")

            display_key = SECTION_HEADINGS.get(section_key, section_key)
            display_sections[display_key] = [str(p.selected_team or p.market_type) for p in _section_picks]
            all_picks.extend(_section_picks)

    if not all_picks:
        error_msg = "Stats-only builder failed: generated sections exist but official_picks is empty." if (_stats_only_mode() and stats_sections_found and not stats_only_builder_picks_created) else "No structured official picks were generated."
        display_sections["_errors"] = errors if errors else [error_msg]

    from datetime import datetime, timezone

    _dd = card_date
    try:
        _dd = datetime.strptime(card_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        _dd = str(card_date or "Unavailable")

    meta: dict[str, Any] = {
        "source_command": source_command,
        "errors": errors,
    }
    if _stats_only_mode():
        meta["market_mode"] = "stats_only"
        meta["stats_builder"] = {
            "sections_found": stats_sections_found,
            "section_item_counts": stats_section_item_counts,
            "official_picks_created": len(all_picks),
        }

    card = StructuredCard(
        card_date=card_date,
        display_date=_dd,
        sport="mlb",
        league="MLB",
        generated_at=datetime.now(timezone.utc).isoformat(),
        official_picks=all_picks,
        display_sections=display_sections,
        metadata=meta,
    )
    return card


# ── Smoke test ──────────────────────────────────────────────────────────────

def _smoke_test_stats_only() -> dict[str, Any]:
    """Verify stats-only builder produces picks from generated sections.

    Run with python -c "from core.builder import _smoke_test_stats_only; print(_smoke_test_stats_only())"
    """
    analysis = (
        "🔥 PLAY OF THE DAY\n"
        "New York Yankees -150\n\n"
        "🏆 TOP 5 MONEYLINE\n"
        "1. Boston Red Sox +120\n"
        "2. Los Angeles Dodgers -130\n"
        "3. Houston Astros -110\n\n"
        "🔥 TOP 5 F5\n"
        "1. Atlanta Braves -115\n"
        "2. Chicago Cubs +105\n\n"
        "🧩 2-LEG SAFE PARLAY\n"
        "✅ New York Yankees\n"
        "✅ Boston Red Sox\n"
    )
    slate = [
        {
            "game_pk": 1001, "game_id": 1001,
            "away_team": "Boston Red Sox", "home_team": "New York Yankees",
            "game_time": "2026-07-09T19:05:00Z",
        },
        {
            "game_pk": 1002, "game_id": 1002,
            "away_team": "Atlanta Braves", "home_team": "Los Angeles Dodgers",
            "game_time": "2026-07-09T20:10:00Z",
        },
        {
            "game_pk": 1003, "game_id": 1003,
            "away_team": "Chicago Cubs", "home_team": "Houston Astros",
            "game_time": "2026-07-09T21:15:00Z",
        },
    ]
    card = build_card_from_analysis(analysis, slate, "2026-07-09", "smoke_test")
    return {
        "official_picks_count": len(card.official_picks),
        "sections_found": list(card.display_sections.keys()) if card.display_sections else [],
        "picks": [{"market_type": p.market_type, "team": p.selected_team, "market_mode": p.market_mode} for p in card.official_picks],
    }
