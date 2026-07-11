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

ADVANCED_BUILDER_TRACE_VERSION = "advanced_market_context_v1"


def _advanced_market_candidates(
    slate: list[dict[str, Any]], card_date: str,
) -> dict[str, Any]:
    """Build official picks from quant-ranked, verified market objects only."""
    candidates: dict[str, list[tuple[float, OfficialPick]]] = {key: [] for key in SECTION_ORDER if key != "parlay"}
    rejected: list[str] = []

    for game in slate:
        game_pk = game.get("game_pk") or game.get("game_id")
        away = str(game.get("away_team") or "")
        home = str(game.get("home_team") or "")
        label = f"{away} @ {home} ({game_pk})"
        quant = _quant_payload(game)
        edge_raw = quant.get("final_edge_score", quant.get("edge_score"))
        try:
            edge = float(edge_raw)
        except (TypeError, ValueError):
            rejected.append(f"{label}: missing quant edge score")
            continue
        if str(quant.get("engine_decision") or "").upper() not in {"QUALIFIED", "PLAY"}:
            rejected.append(f"{label}: quant engine decision={quant.get('engine_decision') or 'missing'}")
            continue
        context = game.get("market_context") if isinstance(game.get("market_context"), dict) else {}
        prices = game.get("best_available_prices") if isinstance(game.get("best_available_prices"), list) else []
        provider = str(context.get("provider") or "")
        if provider not in {"sharpapi", "sharp_api"} or not context.get("line_verified") or not prices:
            rejected.append(f"{label}: Sharp market_context unavailable or unverified")
            continue

        by_market: dict[str, list[dict[str, Any]]] = {}
        for price in prices:
            if isinstance(price, dict):
                by_market.setdefault(str(price.get("market") or ""), []).append(price)

        def choose(rows: list[dict[str, Any]], preferred_team: str | None = None) -> dict[str, Any] | None:
            if preferred_team:
                target = _normalize_team(preferred_team)
                match = next((row for row in rows if target in _normalize_team(str(row.get("outcome") or row.get("description") or ""))), None)
                if match:
                    return match
                return None
            priced = [row for row in rows if isinstance(row.get("price"), (int, float))]
            return min(priced, key=lambda row: float(row["price"])) if priced else (rows[0] if rows else None)

        money = choose(by_market.get("h2h", []))
        selected_team = str(money.get("outcome") or money.get("description") or "") if money else ""
        if _normalize_team(selected_team) not in {_normalize_team(home), _normalize_team(away)}:
            selected_team = ""

        def add(section: str, market_type: str, row: dict[str, Any] | None, *, team: str | None = None) -> None:
            if not row:
                rejected.append(f"{label}: no verified {market_type} market")
                return
            outcome = str(row.get("outcome") or row.get("description") or "")
            picked_team = team
            if market_type not in {"game_total"} and not picked_team:
                picked_team = outcome if _normalize_team(outcome) in {_normalize_team(home), _normalize_team(away)} else None
            line = row.get("point")
            if market_type in {"runline", "game_total", "team_total"} and line is None:
                rejected.append(f"{label}: {market_type} rejected because verified line is missing")
                return
            sportsbook = row.get("bookmaker_key") or row.get("bookmaker") or context.get("sportsbook")
            odds = row.get("price")
            opponent = None
            if picked_team:
                opponent = home if _normalize_team(picked_team) == _normalize_team(away) else away
            pick = OfficialPick(
                pick_id=_pick_id_from_parts([card_date, str(game_pk), section, market_type, picked_team or outcome, str(line)]),
                sport="mlb", league="MLB", card_date=card_date,
                game_pk=int(game_pk) if game_pk is not None else None,
                game_id=int(game_pk) if game_pk is not None else None,
                game_time_et=_game_time_et(game), away_team=away, home_team=home,
                selected_team=picked_team, opponent=opponent, market_type=market_type,
                market_line=line, line=line, odds=odds, posted_line=line, sportsbook=str(sportsbook or ""),
                line_verified=True, edge_score=edge, confidence=quant.get("confidence"),
                risk_level=quant.get("risk_level"), data_quality_grade=quant.get("data_quality_grade"),
                reason=str(quant.get("matchup_summary") or "Quant-ranked verified live market."),
                status="pending", result=None, model_version=quant.get("model_version", "BETGPTAI v21.0"),
                market_mode="live_odds", odds_status="available", market_context_status="matched",
                official_pick_source="advanced_structured_card", source="advanced_structured_card", section=section,
            )
            candidates[section].append((edge, pick))

        add("play_of_day", "play_of_day", money, team=selected_team or None)
        add("moneyline", "moneyline", money, team=selected_team or None)
        add("f5", "f5_moneyline", choose(by_market.get("f5_h2h", []), selected_team), team=selected_team or None)
        add("runline", "runline", choose(by_market.get("spreads", []), selected_team), team=selected_team or None)
        add("totals", "game_total", choose(by_market.get("totals", [])))
        add("team_totals", "team_total", choose(by_market.get("team_totals", []), selected_team), team=selected_team or None)

    picks: list[OfficialPick] = []
    section_counts: dict[str, int] = {}
    for section, ranked in candidates.items():
        ranked.sort(key=lambda item: item[0], reverse=True)
        limit = 1 if section == "play_of_day" else 5
        chosen = [pick for _, pick in ranked[:limit]]
        section_counts[section] = len(chosen)
        picks.extend(chosen)
    parlay_legs = sorted([pick for pick in picks if pick.market_type == "moneyline"], key=lambda pick: pick.edge_score or 0, reverse=True)[:2]
    for leg in parlay_legs:
        leg.parlay_leg = True
    section_counts["safe_parlay_legs"] = len(parlay_legs)
    return {
        "picks": picks,
        "sections_found": [section for section, count in section_counts.items() if count],
        "section_item_counts": section_counts,
        "candidates_found": sum(len(rows) for rows in candidates.values()),
        "rejected_items": rejected,
    }

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


def _strict_builder_debug() -> bool:
    """Raise the stats-only conversion failure instead of recording it.

    Off by default so production admin commands (e.g. /card_debug) never crash
    on a conversion miss. Set STRICT_BUILDER_DEBUG=true to surface it loudly.
    """
    return os.getenv("STRICT_BUILDER_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


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
    quant = game.get("betgptai_quant_v21") or game.get("betgptai_quant_v20") or game.get("betgptai_internal") or {}
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
    BUILDER_TRACE_VERSION = "stats_only_builder_v2_ACTIVE" if _stats_only_mode() else ADVANCED_BUILDER_TRACE_VERSION

    all_picks: list[OfficialPick] = []
    display_sections: dict[str, list[str]] = {}
    errors: list[str] = []
    stats_only_builder_picks_created = 0
    stats_sections_found: list[str] = []
    stats_section_item_counts: dict[str, int] = {}
    advanced_candidates_found = 0
    advanced_sections_found: list[str] = []

    stats_mode = _stats_only_mode() and bool(slate)

    if stats_mode:
        # Stats-only mode: build picks from sections directly (no odds required).
        # This is the active path when STATS_ONLY_CARD_MODE=true and a slate exists.
        stats_result = _build_stats_only_official_picks(analysis, slate, card_date)
        all_picks = stats_result["picks"]
        stats_sections_found = stats_result.get("sections_found", [])
        stats_section_item_counts = stats_result.get("section_item_counts", {})
        reasons = stats_result.get("rejection_reasons", [])
        if reasons:
            errors.extend(reasons[:20])
        stats_only_builder_picks_created = len(all_picks)
        for pk in all_picks:
            mt = pk.market_type
            label = {"moneyline": "Moneyline", "f5_moneyline": "F5", "runline": "Runline",
                     "total": "Total", "team_total": "Team Total", "parlay": "Parlay"}.get(mt, mt)
            display_sections.setdefault(label, []).append(str(pk.selected_team or ""))
    else:
        advanced = _advanced_market_candidates(slate, card_date)
        all_picks = advanced["picks"]
        advanced_candidates_found = int(advanced.get("candidates_found") or 0)
        advanced_sections_found = list(advanced.get("sections_found") or [])
        errors.extend(list(advanced.get("rejected_items") or [])[:50])
        for pick in all_picks:
            display_sections.setdefault(pick.section or pick.market_type, []).append(
                str(pick.selected_team or pick.market_type)
            )

    # Explicit fallback (spec point 5): if stats-only mode is active and the
    # active path above produced no picks, run the stats-only builder helper
    # directly. This must happen BEFORE StructuredCard(...) is returned.
    if _stats_only_mode() and not all_picks and slate:
        stats_result = _build_stats_only_official_picks(analysis, slate, card_date)
        if stats_result["picks"]:
            all_picks = stats_result["picks"]
            stats_sections_found = stats_result.get("sections_found", [])
            stats_section_item_counts = stats_result.get("section_item_counts", {})
            reasons = stats_result.get("rejection_reasons", [])
            if reasons:
                errors.extend(reasons[:20])
            stats_only_builder_picks_created = len(all_picks)
            for pk in all_picks:
                mt = pk.market_type
                label = {"moneyline": "Moneyline", "f5_moneyline": "F5", "runline": "Runline",
                         "total": "Total", "team_total": "Team Total", "parlay": "Parlay"}.get(mt, mt)
                display_sections.setdefault(label, []).append(str(pk.selected_team or ""))

    # Conversion failure signal: generated sections exist but stats-only
    # conversion still returned 0 picks. In STRICT_BUILDER_DEBUG mode this is
    # raised before returning StructuredCard so the failure is loud. Otherwise
    # it is recorded in metadata so admin commands keep working and the simple
    # engine fallback remains available.
    conversion_failed = bool(_stats_only_mode() and not all_picks and stats_sections_found)
    conversion_error = ""
    if conversion_failed:
        conversion_error = "ACTIVE builder stats-only conversion failed before StructuredCard return"
        if _strict_builder_debug():
            raise RuntimeError(conversion_error)

    if not all_picks:
        error_msg = "Stats-only builder found 0 sections to convert." if _stats_only_mode() else "No structured official picks were generated."
        display_sections["_errors"] = errors if errors else [error_msg]

    from datetime import datetime, timezone

    _dd = card_date
    try:
        _dd = datetime.strptime(card_date, "%Y-%m-%d").strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        _dd = str(card_date or "Unavailable")

    meta: dict[str, Any] = {
        "builder_trace_version": BUILDER_TRACE_VERSION,
        "source_command": source_command,
        "errors": errors,
        "builder_conversion_failed": conversion_failed,
        "builder_conversion_error": conversion_error,
        "sections_found": stats_sections_found if stats_mode else advanced_sections_found,
        "candidates_found": len(all_picks) if stats_mode else advanced_candidates_found,
        "official_picks_created": len(all_picks),
        "rejected_items": errors,
        "rejected_reasons": errors,
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
