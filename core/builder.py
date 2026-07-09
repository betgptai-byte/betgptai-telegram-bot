"""Build a StructuredCard from AI analysis + slate data.

No Telegram-formatted-text parsing.  Uses section headings (the structural
contract between prompt and builder) to identify picks, then enriches each
pick with game data, quant scores, and odds from the slate.
"""
from __future__ import annotations

import hashlib
import uuid
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


def build_card_from_analysis(
    analysis: str,
    slate: list[dict[str, Any]],
    card_date: str,
    source_command: str = "unknown",
) -> StructuredCard:
    headings = _find_headings(analysis)
    all_picks: list[OfficialPick] = []
    display_sections: dict[str, list[str]] = {}
    errors: list[str] = []

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
        display_sections["_errors"] = errors if errors else ["No structured official picks were generated."]

    from datetime import datetime, timezone

    _dd = card_date
    try:
        _dd = datetime.strptime(card_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        _dd = str(card_date or "Unavailable")

    card = StructuredCard(
        card_date=card_date,
        display_date=_dd,
        sport="mlb",
        league="MLB",
        generated_at=datetime.now(timezone.utc).isoformat(),
        official_picks=all_picks,
        display_sections=display_sections,
        metadata={
            "source_command": source_command,
            "errors": errors,
            "headings_found": [h[0] for h in headings],
        },
    )
    return card
