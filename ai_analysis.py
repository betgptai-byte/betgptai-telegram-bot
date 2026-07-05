"""Send a combined MLB slate to OpenAI for concise analysis."""

from __future__ import annotations

import json
import os
import re
import sys
import traceback
from typing import Any

from openai import AsyncOpenAI

from card_format import PARLAY_NOTE
from card_time import eastern_now
from game_time import game_sort_key, mlb_game_block, parse_game_time
from consensus_analysis import (
    analyze_slate_with_claude,
    analyze_specialized_with_claude,
    evidence_for_pick,
    public_confidence_summary,
)
from model_engines import slate_engine_summary, value_context_for_pick
from quant_engine import enrich_slate_with_quant_scores


DIVIDER = "━━━━━━━━━━━━"
NUMBER_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")
FREE_CARD_FOOTER = f"""{DIVIDER}

💎 WANT THE FULL BETGPTAI PREMIUM CARD?

Premium includes:

✅ Full MLB slate analysis
✅ Top 5 Moneyline plays
✅ Top 5 Runline plays
✅ Top 5 Totals
✅ Team totals
✅ F5 leans
✅ Weather + park factor edges
✅ SP vs SP matchup breakdown
✅ Bullpen + trend analysis
✅ Premium parlay card

Type /vip to unlock the full card.

{DIVIDER}

📌 Odds vary by sportsbook. Shop for the best available number.

Tap the Disclaimer button for BETGPTAI notes and responsible-play info."""


class AIAnalysisError(Exception):
    """A friendly error raised when OpenAI cannot create the analysis."""


_LAST_ANALYSIS_METADATA: dict[str, Any] = {}


def get_last_analysis_metadata() -> dict[str, Any]:
    """Return model audit details for owner-only reports."""
    return dict(_LAST_ANALYSIS_METADATA)


def _remember_analysis_metadata(
    openai_used: bool, claude_used: bool, agreement: bool, fallback_used: bool,
    slate: list[dict[str, Any]] | None = None,
) -> None:
    engine_summary = slate_engine_summary(slate or [])
    _LAST_ANALYSIS_METADATA.clear()
    _LAST_ANALYSIS_METADATA.update({
        "openai_used": openai_used,
        "claude_used": claude_used,
        "agreement": agreement,
        "consensus_picks_found": 1 if agreement else 0,
        "fallback_used": fallback_used,
        "value_engine_count": engine_summary.get("value_engine_count", 0),
        "nrfi_candidates": engine_summary.get("nrfi_candidates", 0),
        "f5_candidates": engine_summary.get("f5_candidates", 0),
        "team_total_candidates": engine_summary.get("team_total_candidates", 0),
        "strikeout_candidates": engine_summary.get("strikeout_candidates", 0),
        "home_run_candidates": engine_summary.get("home_run_candidates", 0),
    })


SPECIALIZED_MLB_CARDS = {
    "f5": ("🔥 F5 MONEYLINE LEAN", "F5 moneyline only; never F5 totals or runlines"),
    # Legacy/specialized path only. NRFI/YRFI are intentionally excluded from
    # official v20 cards.
    "nrfi": ("⚾ NRFI LEAN", "NRFI only"),
    "teamtotals": ("💰 TEAM TOTAL ANGLE", "team total only"),
    "parlay": ("🧩 SAFE PARLAY OF THE DAY", "two or three safer standalone legs; no guarantees"),
    "fullday": ("🔥 FULL DAY MLB CARD", "best full-day MLB card across supported markets"),
    "strikeouts": ("🔥 STRIKEOUT ANGLE", "pitcher strikeouts only"),
    "hits": ("⚾ HITS ANGLE", "batter hits only"),
    "home_runs": ("💥 HOME RUN ANGLE", "batter home runs only"),
}


def _model_safe_slate(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hide sportsbook identity while retaining the best computed numbers."""
    return [
        {key: value for key, value in game.items() if key != "bookmakers"}
        for game in slate
    ]


def _risk_to_confidence(match: re.Match[str]) -> str:
    """Convert legacy risk labels into member-facing confidence labels."""
    try:
        risk = float(match.group(1))
    except ValueError:
        return "🎯 Confidence Grade: 6/10"
    confidence = max(5, min(9, round(11 - risk)))
    return f"🎯 Confidence Grade: {confidence}/10"


def _apply_public_confidence(
    card: str, grade: int, value_note: str
) -> str:
    """Hide model/provider internals and show only confidence/value wording."""
    del value_note
    cleaned = re.sub(r"(?im)^Risk Grade:\s*(\d+(?:\.\d+)?)/10", _risk_to_confidence, card)
    cleaned = re.sub(
        r"(?i)\n?🤖 AI (?:CONSENSUS EDGE|SPLIT OPINION).*?(?=\n━━━━━━━━━━━━|\Z)",
        "",
        cleaned,
        flags=re.S,
    )
    cleaned = re.sub(r"(?i)^.*(?:OpenAI|Claude|Consensus|AI disagreement).*$\n?", "", cleaned, flags=re.M)
    cleaned = re.sub(
        r"🎯 Confidence Grade:\s*\d+(?:\.\d+)?/10",
        f"🎯 Confidence Grade: {grade}/10",
        cleaned,
        count=1,
    )
    cleaned = re.sub(r"(?ims)^📈 Value Note:\s*\n?.*?(?=\n\n|$)", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


async def analyze_specialized_mlb_slate(
    slate: list[dict[str, Any]], api_key: str, card_type: str,
    anthropic_api_key: str = "",
) -> str:
    """Create one Savant-aware premium market card without inventing a line."""
    if card_type not in SPECIALIZED_MLB_CARDS:
        raise AIAnalysisError(f"Unknown specialized MLB card: {card_type}")
    heading, market_rule = SPECIALIZED_MLB_CARDS[card_type]
    if not slate:
        return "No MLB games were found for today."

    market_names = {price.get("market") for game in slate for price in game.get("best_available_prices", [])}
    required_market = {
        "teamtotals": {"team_totals", "alternate_team_totals"},
        "strikeouts": {"pitcher_strikeouts", "pitcher_strikeouts_alternate"},
        "hits": {"batter_hits", "batter_hits_alternate"},
        "home_runs": {"batter_home_runs"},
    }.get(card_type)
    if required_market and not (market_names & required_market):
        label = {
            "teamtotals": "Team-total",
            "strikeouts": "Pitcher strikeout",
            "hits": "Batter hit",
            "home_runs": "Home-run",
        }[card_type]
        return (
            f"{heading}\n\n{label} markets unavailable from current odds feed.\n\n"
            "📋 DATA LIMITATIONS\n\nNo verified market line is available, so no pick was invented.\n\n"
            f"{TIMED_CARD_FOOTER}"
        )
    simple_label = {
        "nrfi": "NRFI Lean: [Team/Game]",
        "strikeouts": "Strikeout Lean: [Pitcher Over/Under X Ks]",
        "home_runs": "HR Watch: [Player Name]",
        "f5": "[Team] F5 ML",
        "teamtotals": "[Team] Team Total [Over/Under] [line]",
        "hits": "Hits Lean: [Player Over/Under X Hits]",
    }[card_type]
    prompt = f"""
Create exactly one {market_rule} recommendation from this MLB slate.

{json.dumps(_model_safe_slate(slate), indent=2, default=str)}

Weight predictive metrics more heavily than traditional results:
- xERA, xwOBA, Barrel %, and Whiff % are primary.
- Hard Hit %, exit velocity, chase rate, sweet-spot rate, launch angle,
  fastball velocity, and pitch-type matchup are secondary.
- Use ERA and recent form only as supporting context.

Market emphasis:
- F5 and NRFI: starter xERA, Whiff %, chase, fastball velocity, opposing team
  xwOBA versus handedness, and pitch-type fit.
- Team totals: team xwOBA, Barrel %, Hard Hit %, bullpen context, park/weather.
- Strikeouts: Whiff %, chase, pitch arsenal, velocity, and opponent contact.
- Hits/home runs: batter xwOBA, Barrel %, Hard Hit %, exit velocity, launch
  angle, pitcher contact allowed, pitch-type fit, park and weather.

Use only a line that appears in best_available_prices. Never display a
sportsbook name. If evidence or the requested market is unavailable, say so;
do not invent a player, line, split, or metric. Output this compact format:

{heading}

{simple_label}
Risk Grade: [5-8]/10
Reason: [one short sentence max]

━━━━━━━━━━━━

📋 DATA LIMITATIONS
[one short bullet list only when needed]

Do not add a disclaimer. Never show American odds, sportsbook names, raw model
scoring, provider names, formulas, or raw Savant metric details. Never say
guaranteed, lock, sure win, or 99.9%.
""".strip()
    openai_text: str | None = None
    primary_pick: dict[str, Any] | None = None
    try:
        if not api_key:
            raise AIAnalysisError("OPENAI_API_KEY is missing from .env.")
        response = await AsyncOpenAI(api_key=api_key).responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            instructions=(
                "You are a careful Statcast-first MLB analyst. Use supplied data only, "
                "return Telegram-ready plain text, and hide all sportsbook identity."
            ),
            input=prompt,
        )
        openai_text = response.output_text.strip()
        if not openai_text or heading not in openai_text:
            raise AIAnalysisError("OpenAI returned an incomplete specialized card.")
        openai_text = _sanitize_telegram_output(openai_text, slate)
        openai_text = re.sub(
            r"Educational analysis only\.\s*Play responsibly\.", "", openai_text, flags=re.I
        ).strip()
        primary_pick = _specialized_pick_from_card(openai_text, card_type)
    except Exception as error:
        print(f"OpenAI Error:\n{error}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)

    try:
        claude_pick = await analyze_specialized_with_claude(
            _model_safe_slate(slate), anthropic_api_key, card_type
        )
    except Exception as error:
        print(f"Claude Error:\n{error}", file=sys.stderr, flush=True)
        claude_pick = None

    if openai_text is None and claude_pick is None:
        return f"{heading}\n\nAnalysis is temporarily unavailable.\n\n{TIMED_CARD_FOOTER}"
    if openai_text is None:
        claude_grade = claude_pick.get("risk_grade", 7)
        if not isinstance(claude_grade, (int, float)):
            claude_grade = 7
        openai_text = (
            f"{heading}\n\n{claude_pick.get('selection', 'Analysis unavailable')}\n"
            f"Risk Grade: {claude_grade:g}/10\n"
            f"Reason: {claude_pick.get('reason', 'Claude second opinion only.')}"
        )
    evidence = evidence_for_pick(slate, primary_pick or claude_pick, "mlb")
    confidence = public_confidence_summary(primary_pick, claude_pick, evidence)
    public_card = _apply_public_confidence(
        openai_text, confidence["grade"], confidence["value_note"]
    )
    return f"{public_card}\n\n{TIMED_CARD_FOOTER}"


def _specialized_pick_from_card(text: str, card_type: str) -> dict[str, Any] | None:
    """Extract one specialized OpenAI lean for model-to-model comparison."""
    heading = SPECIALIZED_MLB_CARDS[card_type][0]
    body = text.split(heading, 1)[-1].strip()
    selection = next(
        (line.strip() for line in body.splitlines()
         if line.strip() and not line.startswith(("Line:", "Risk Grade:", "Reason:", "━", "📋"))),
        None,
    )
    if not selection:
        return None
    line_match = re.search(r"(?m)^Line:\s*([+-]?\d+(?:\.\d+)?)", body)
    line: int | float | None = None
    if line_match:
        line = float(line_match.group(1))
        line = int(line) if line.is_integer() else line
    return {
        "selection": selection,
        "market": card_type,
        "line": line,
        "implied_probability": _implied_probability(line) if isinstance(line, (int, float)) else None,
        "estimated_probability": None,
    }


def _implied_probability(american_odds: int | float) -> float:
    """Convert American odds into a simple market-implied probability."""
    if american_odds < 0:
        return abs(american_odds) / (abs(american_odds) + 100)
    return 100 / (american_odds + 100)


def _format_american_odds(price: int | float) -> str:
    """Add the plus sign used when displaying positive American odds."""
    return f"+{price:g}" if price > 0 else f"{price:g}"


def _fallback_candidates(
    slate: list[dict[str, Any]], market_key: str
) -> list[dict[str, Any]]:
    """Build ranked fallback candidates from the precomputed best prices."""
    candidates: list[dict[str, Any]] = []
    for game in slate:
        quant = game.get("betgptai_quant_v20")
        if isinstance(quant, dict) and quant.get("engine_decision") == "PASS":
            continue
        for wager in game.get("best_available_prices", []):
            price = wager.get("price")
            if wager.get("market") != market_key or not isinstance(
                price, (int, float)
            ):
                continue
            if market_key == "h2h" and price <= -190:
                continue
            candidates.append(
                {
                    **wager,
                    "game_id": game.get("game_id"),
                    "away_team": game.get("away_team", "Unknown"),
                    "home_team": game.get("home_team", "Unknown"),
                    "away_pitcher": game.get("away_pitcher", "TBD"),
                    "home_pitcher": game.get("home_pitcher", "TBD"),
                    "game_time": game.get("game_time"),
                    "status": game.get("status"),
                    "implied_probability": _implied_probability(price),
                }
            )

    # Lower-priced favorites rank above long shots in this data-only fallback.
    candidates.sort(key=lambda item: item["implied_probability"], reverse=True)

    # Avoid listing several alternate lines for the same outcome in one game.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in candidates:
        identity = (candidate["game_id"], candidate["market"])
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    return unique


def upcoming_mlb_slate(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only games that have not started yet for public free cards."""
    now = eastern_now()
    upcoming: list[dict[str, Any]] = []
    for game in slate:
        status_text = str(game.get("status") or "").lower()
        if any(word in status_text for word in ("live", "in progress", "warmup", "final", "game over")):
            continue
        parsed = parse_game_time(game.get("game_time"))
        if parsed is not None and parsed <= now:
            continue
        upcoming.append(game)
    return upcoming


def _pick_name(candidate: dict[str, Any]) -> str:
    """Turn an odds outcome into a readable betting pick."""
    point = candidate.get("point")
    if candidate["market"] == "spreads" and isinstance(point, (int, float)):
        return f"{candidate['outcome']} {point:+g}"
    if candidate["market"] == "totals" and isinstance(point, (int, float)):
        return (
            f"{candidate['outcome']} {point:g} "
            f"({candidate['away_team']} @ {candidate['home_team']})"
        )
    return str(candidate["outcome"])


def _format_fallback_pick(
    candidate: dict[str, Any], number: int | None = None, featured: bool = False
) -> str:
    """Format one deterministic fallback selection for Telegram."""
    probability = candidate["implied_probability"]
    risk_grade = max(1, min(10, round(10 - probability * 9)))
    if featured:
        prefix = "⚾ "
    elif number is not None and 1 <= number <= len(NUMBER_EMOJIS):
        prefix = f"{NUMBER_EMOJIS[number - 1]} "
    else:
        prefix = "⚾ "
    return (
        f"{prefix}{_pick_name(candidate)}\n"
        f"Risk Grade: {risk_grade}/10"
    )


def _format_fallback_list(candidates: list[dict[str, Any]], limit: int = 2) -> str:
    """Format up to a requested number of market-ranked selections."""
    selected = sorted(
        candidates[:limit],
        key=lambda candidate: game_sort_key(candidate),
    )
    if not selected:
        return "No qualified play — usable odds are missing."
    return "\n\n".join(
        _format_fallback_pick(candidate, index)
        for index, candidate in enumerate(selected, start=1)
    )


def _format_team_total_pick(candidate: dict[str, Any], number: int | None = None) -> str:
    """Format one real team-total market with a one-run safer alternate."""
    prefix = f"{NUMBER_EMOJIS[number - 1]} " if number else ""
    team = candidate.get("description") or candidate.get("team")
    direction = str(candidate.get("outcome", "")).title()
    point = candidate.get("point")
    price = candidate.get("price")
    if (
        not team
        or direction not in {"Over", "Under"}
        or not isinstance(point, (int, float))
        or not isinstance(price, (int, float))
    ):
        return ""
    safer_point = point - 1 if direction == "Over" else point + 1
    probability = candidate["implied_probability"]
    risk_grade = max(1, min(10, round(10 - probability * 9)))
    return (
        f"{prefix}{team} Team Total {direction} {point:g}\n"
        f"Safer Alt: {direction} {safer_point:g}\n"
        f"Risk Grade: {risk_grade}/10"
    )


def _infer_safe_team_total_picks(slate: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    """Create conservative team-total displays when the official market is absent.

    These are display-safe defaults, not sportsbook-specific lines. We only use
    them when a side can be inferred from available moneyline/total context.
    """
    inferred: list[dict[str, Any]] = []
    moneylines = _fallback_candidates(slate, "h2h")
    totals = _fallback_candidates(slate, "totals")
    by_game_moneylines: dict[Any, list[dict[str, Any]]] = {}
    for pick in moneylines:
        by_game_moneylines.setdefault(pick.get("game_id"), []).append(pick)
    by_game_totals: dict[Any, list[dict[str, Any]]] = {}
    for pick in totals:
        by_game_totals.setdefault(pick.get("game_id"), []).append(pick)

    for game in slate:
        game_id = game.get("game_id")
        ml = sorted(
            by_game_moneylines.get(game_id, []),
            key=lambda item: item.get("implied_probability") or 0,
            reverse=True,
        )
        if not ml:
            continue
        total_side = next(
            (
                item for item in by_game_totals.get(game_id, [])
                if str(item.get("outcome", "")).lower() in {"over", "under"}
            ),
            None,
        )
        if total_side and str(total_side.get("outcome", "")).lower() == "under" and len(ml) > 1:
            # Low-scoring game context: safer default is the weaker side under.
            team = ml[-1].get("outcome")
            inferred.append({
                "team": team,
                "direction": "Under",
                "line": 5.5,
                "safer": 6.5,
                "risk_grade": 6,
            })
        else:
            # Stronger team/offense context: safer default is favorite over.
            team = ml[0].get("outcome")
            inferred.append({
                "team": team,
                "direction": "Over",
                "line": 4.5,
                "safer": 3.5,
                "risk_grade": 6,
            })
        if len(inferred) >= limit:
            break
    return [item for item in inferred if item.get("team")]


def _format_team_total_list(slate: list[dict[str, Any]], limit: int = 2) -> str:
    """Format team totals with safe defaults when official team totals are absent."""
    candidates = _fallback_candidates(slate, "team_totals")
    formatted: list[str] = []
    if candidates:
        formatted = [
            text for index, candidate in enumerate(candidates[:limit], start=1)
            if (text := _format_team_total_pick(candidate, index))
        ]
    if not formatted:
        inferred = _infer_safe_team_total_picks(slate, limit)
        formatted = [
            (
                f"{NUMBER_EMOJIS[index - 1]} {pick['team']} Team Total {pick['direction']} {pick['line']:g}\n"
                f"Safer Alt: {pick['direction']} {pick['safer']:g}\n"
                f"Risk Grade: {pick['risk_grade']}/10"
            )
            for index, pick in enumerate(inferred, start=1)
        ]
    if not formatted:
        return "Team-total side unavailable from current feed."
    return "\n\n".join(formatted)


def _format_f5_moneyline_list(
    moneylines: list[dict[str, Any]], limit: int = 2,
) -> str:
    """Create early-game moneyline leans without inventing an F5 price."""
    if not moneylines:
        return "No qualified F5 moneyline lean is available."
    formatted = []
    for index, pick in enumerate(moneylines[:limit], start=1):
        probability = pick["implied_probability"]
        risk_grade = max(1, min(10, round(10 - probability * 9)))
        formatted.append(
            f"{NUMBER_EMOJIS[index - 1]} {pick['outcome']} F5 ML\n"
            f"Risk Grade: {risk_grade}/10"
        )
    return "\n\n".join(formatted)


def build_fallback_card(slate: list[dict[str, Any]]) -> str:
    """Create a no-AI card using only MLB schedule and sportsbook odds data."""
    moneylines = _fallback_candidates(slate, "h2h")
    spreads = _fallback_candidates(slate, "spreads")
    totals = _fallback_candidates(slate, "totals")

    play_of_day = (
        _format_fallback_pick(moneylines[0], featured=True)
        if moneylines
        else "No qualified play — moneyline odds are missing."
    )

    # Use two different games for the free market-favorite parlay fallback.
    parlay_legs: list[dict[str, Any]] = []
    used_games: set[Any] = set()
    for candidate in moneylines:
        if candidate["game_id"] not in used_games:
            used_games.add(candidate["game_id"])
            parlay_legs.append(candidate)
        if len(parlay_legs) == 2:
            break

    if len(parlay_legs) == 2:
        parlay_legs.sort(key=game_sort_key)
        parlay = "\n".join(
            f"✅ {_pick_name(leg)}" for leg in parlay_legs
        )
    else:
        parlay = "No qualified two-leg parlay — fewer than two usable games."

    card = f"""🔥 PLAY OF THE DAY

{play_of_day}

{DIVIDER}

🏆 TOP 5 MONEYLINE

{_format_fallback_list(moneylines, 5)}

{DIVIDER}

🔥 TOP 5 F5

{_format_f5_moneyline_list(moneylines, 5)}

{DIVIDER}

📈 TOP 5 RUN LINE

{_format_fallback_list(spreads, 5)}

{DIVIDER}

🎯 TOP 5 GAME TOTALS

{_format_fallback_list(totals, 5)}

{DIVIDER}

💰 TOP 5 TEAM TOTALS

{_format_team_total_list(slate, 5)}

{DIVIDER}

🧩 2-LEG SAFE PARLAY

{parlay}

{FREE_CARD_FOOTER}"""
    return _add_mlb_game_context(_sort_mlb_card_picks(card, slate), slate)


def _normalized_team(value: Any) -> str:
    """Normalize a team label for matching AI selections back to the slate."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _game_for_selection(
    selection: str, slate: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Find the one scheduled game referenced by a displayed pick."""
    normalized_selection = _normalized_team(selection)
    matches: list[dict[str, Any]] = []
    for game in slate:
        team_tokens = []
        for key in ("away_team", "home_team"):
            team = str(game.get(key, ""))
            full = _normalized_team(team)
            nickname = _normalized_team(team.split()[-1] if team.split() else "")
            team_tokens.extend(token for token in (full, nickname) if len(token) >= 3)
        if any(token in normalized_selection for token in team_tokens):
            matches.append(game)
    if len(matches) == 1:
        return matches[0]
    # If both team names are printed, duplicate nickname matches still represent
    # the same scheduled game; preserve the first unique game ID.
    unique = {game.get("game_id"): game for game in matches}
    return next(iter(unique.values())) if len(unique) == 1 else None


def _add_mlb_game_context(text: str, slate: list[dict[str, Any]]) -> str:
    """Insert deterministic matchup/time blocks beneath every displayed pick."""
    relevant_headings = {
        "🔥 PLAY OF THE DAY", "🏆 TOP 5 MONEYLINE", "🔥 TOP 5 F5",
        "📈 TOP 5 RUN LINE", "🎯 TOP 5 GAME TOTALS",
        "💰 TOP 5 TEAM TOTALS", "🧩 2-LEG SAFE PARLAY",
    }
    chunks = text.split(DIVIDER)
    decorated: list[str] = []
    for chunk in chunks:
        heading = next((item for item in relevant_headings if item in chunk), None)
        if not heading:
            decorated.append(chunk)
            continue
        lines = chunk.splitlines()
        output: list[str] = []
        for index, line in enumerate(lines):
            output.append(line)
            stripped = line.strip()
            is_numbered = stripped.startswith(("⚾ ", "✅ ", *tuple(f"{emoji} " for emoji in NUMBER_EMOJIS)))
            is_f5 = heading == "🔥 TOP 5 F5" and stripped.upper().endswith("F5 ML")
            is_team_total = (
                heading == "💰 TOP 5 TEAM TOTALS"
                and "team total" in stripped.lower()
                and "unavailable" not in stripped.lower()
            )
            if not (is_numbered or is_f5 or is_team_total):
                continue
            if index + 1 < len(lines) and lines[index + 1].strip().startswith("🆚"):
                continue
            game = _game_for_selection(stripped, slate)
            if game:
                output.extend(mlb_game_block(game).splitlines())
        decorated.append("\n".join(output))
    return f"\n\n{DIVIDER}\n\n".join(chunk.strip() for chunk in decorated if chunk.strip())


def _sort_mlb_card_picks(text: str, slate: list[dict[str, Any]]) -> str:
    """Sort every multi-pick MLB section by its scheduled Eastern start."""
    chunks = text.split(DIVIDER)
    sorted_chunks: list[str] = []
    list_headings = {
        "🏆 TOP 5 MONEYLINE", "📈 TOP 5 RUN LINE",
        "🎯 TOP 5 GAME TOTALS", "🔥 TOP 5 F5",
        "💰 TOP 5 TEAM TOTALS",
    }
    for chunk in chunks:
        heading = next((value for value in list_headings if value in chunk), None)
        lines = chunk.splitlines()
        if heading:
            starts = [
                index for index, line in enumerate(lines)
                if line.strip().startswith(NUMBER_EMOJIS)
            ]
            if len(starts) >= 2:
                prefix = lines[:starts[0]]
                blocks = [
                    lines[start: starts[index + 1] if index + 1 < len(starts) else len(lines)]
                    for index, start in enumerate(starts)
                ]
                blocks.sort(
                    key=lambda block: game_sort_key(
                        _game_for_selection(block[0], slate) or {}
                    )
                )
                rebuilt: list[str] = []
                for number, block in enumerate(blocks, start=1):
                    if number <= len(NUMBER_EMOJIS):
                        block[0] = re.sub(
                            r"^(?:1️⃣|2️⃣|3️⃣|4️⃣|5️⃣)",
                            NUMBER_EMOJIS[number - 1],
                            block[0],
                        )
                    rebuilt.extend(block)
                lines = prefix + rebuilt
        elif "🧩 2-LEG SAFE PARLAY" in chunk:
            indexes = [
                index for index, line in enumerate(lines)
                if line.strip().startswith("✅ ")
            ][:2]
            if len(indexes) == 2:
                leg_lines = [lines[index] for index in indexes]
                leg_lines.sort(
                    key=lambda line: game_sort_key(
                        _game_for_selection(line, slate) or {}
                    )
                )
                for index, leg_line in zip(indexes, leg_lines):
                    lines[index] = leg_line
        sorted_chunks.append("\n".join(lines))
    return f"\n\n{DIVIDER}\n\n".join(chunk.strip() for chunk in sorted_chunks if chunk.strip())


def _sanitize_telegram_output(text: str, slate: list[dict[str, Any]]) -> str:
    """Remove sportsbook names and normalize pick labels before sending output."""
    sportsbook_names: set[str] = set()
    for game in slate:
        for bookmaker in game.get("bookmakers", []):
            for value in (bookmaker.get("name"), bookmaker.get("key")):
                if isinstance(value, str) and value.strip():
                    sportsbook_names.add(value.strip())
        for price in game.get("best_available_prices", []):
            for value in (price.get("bookmaker"), price.get("bookmaker_key")):
                if isinstance(value, str) and value.strip():
                    sportsbook_names.add(value.strip())

    cleaned = text
    # Remove longer names first in case one sportsbook name contains another.
    for name in sorted(sportsbook_names, key=len, reverse=True):
        cleaned = re.sub(re.escape(name), "", cleaned, flags=re.IGNORECASE)

    cleaned = re.sub(
        r"(?im)^Best(?: available)? odds(?:/bookmaker)?\s*:",
        "Line:",
        cleaned,
    )
    cleaned = re.sub(r"(?im)^Risk grade\s*:", "Risk Grade:", cleaned)

    # Normalize plain headings if the model forgets their display emojis.
    heading_emojis = {
        "PLAY OF THE DAY": "🔥 PLAY OF THE DAY",
        "TOP 5 MONEYLINE": "🏆 TOP 5 MONEYLINE",
        "TOP 5 F5": "🔥 TOP 5 F5",
        "TOP 5 F5 MONEYLINE": "🔥 TOP 5 F5",
        "TOP 5 RUN LINE": "📈 TOP 5 RUN LINE",
        "TOP 5 RUNLINE/SPREAD": "📈 TOP 5 RUN LINE",
        "TOP 5 GAME TOTALS": "🎯 TOP 5 GAME TOTALS",
        "TOP 5 OVER/UNDER TOTAL RUNS": "🎯 TOP 5 GAME TOTALS",
        "TOP 5 TEAM TOTALS": "💰 TOP 5 TEAM TOTALS",
        "TOP 2 MONEYLINE": "🏆 TOP 5 MONEYLINE",
        "TOP 2 F5 MONEYLINE": "🔥 TOP 5 F5",
        "F5 MONEYLINE LEAN": "🔥 TOP 5 F5",
        "TOP 2 RUNLINE/SPREAD": "📈 TOP 5 RUN LINE",
        "TOP 2 OVER/UNDER TOTAL RUNS": "🎯 TOP 5 GAME TOTALS",
        "TOP 2 TEAM TOTALS": "💰 TOP 5 TEAM TOTALS",
        "TEAM TOTAL ANGLE": "💰 TOP 5 TEAM TOTALS",
        "2-LEG SAFE PARLAY": "🧩 2-LEG SAFE PARLAY",
    }
    for heading, decorated_heading in heading_emojis.items():
        cleaned = re.sub(
            rf"(?im)^{re.escape(heading)}$", decorated_heading, cleaned
        )
    cleaned = re.sub(r"(?im)^🔥 TOP 5 F5 MONEYLINE$", "🔥 TOP 5 F5", cleaned)
    cleaned = re.sub(r"(?im)^📈 TOP 5 RUNLINE/SPREAD$", "📈 TOP 5 RUN LINE", cleaned)
    cleaned = re.sub(r"(?im)^🎯 TOP 5 OVER/UNDER TOTAL RUNS$", "🎯 TOP 5 GAME TOTALS", cleaned)

    # Member cards should not display American odds because prices vary by
    # sportsbook and state. Market numbers stay inside the pick text itself
    # for totals, spreads, and team totals.
    cleaned = re.sub(r"(?im)^Line:\s*[+-]?\d+(?:\.\d+)?(?:\s+.*)?\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^Line:\s*(?:Market line unavailable|Unavailable|Not available)\s*\n?", "", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)

    # Remove betting-certainty language even if it slips through the prompt.
    cleaned = re.sub(r"(?i)\bguaranteed\b", "certain", cleaned)
    cleaned = re.sub(r"(?i)\bsure win\b", "high-confidence edge", cleaned)
    cleaned = re.sub(r"(?i)99\.9\s*%", "high confidence", cleaned)
    cleaned = re.sub(r"(?i)\block\b", "pick", cleaned)
    # Model-written prose never gets to self-certify expected value. The
    # consensus panel is the only place probability inputs are evaluated.
    cleaned = re.sub(r"(?i)\+EV", "potential value angle", cleaned)
    cleaned = re.sub(
        r"(?i)^.*(?:AI Split Opinion|OpenAI Lean|Claude Lean|Claude Unavailable|"
        r"OpenAI Unavailable|Consensus \d/2|AI disagreement).*$\n?",
        "",
        cleaned,
        flags=re.M,
    )
    # Member cards should feel like a sports app, not an API/model report.
    provider_replacements = {
        r"\bOpenAI\b": "our models",
        r"\bClaude\b": "our models",
        r"\bAnthropic\b": "our models",
        r"\bBaseball Savant\b": "advanced matchup data",
        r"\bStatcast\b": "advanced matchup data",
        r"\bpybaseball\b": "advanced matchup data",
        r"\bFanGraphs\b": "advanced matchup data",
        r"\bMLB Stats API\b": "schedule data",
        r"\bThe Odds API\b": "market data",
        r"\bOdds API\b": "market data",
        r"\bHighlightly\b": "news data",
        r"\bOpen-Meteo\b": "weather data",
        r"\bTheSportsDB\b": "soccer data",
        r"\bClubElo\b": "team-strength data",
        r"\bUnderstat\b": "advanced soccer data",
        r"\bSerpApi\b": "backup context",
        r"\bStatsBomb\b": "advanced soccer data",
        r"\bFBref\b": "soccer trend data",
        r"\bAPI-Football\b": "soccer data",
        r"\bAPI Sports\b": "soccer data",
    }
    for pattern, replacement in provider_replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    metric_replacements = {
        r"\bxERA\b": "starting-pitching profile",
        r"\bxwOBA\b": "contact profile",
        r"\bBarrel\s*%\b": "power-contact profile",
        r"\bHard\s*Hit\s*%\b": "hard-contact profile",
        r"\bWhiff\s*%\b": "swing-and-miss profile",
        r"\bChase\s*%\b": "plate-discipline profile",
    }
    for pattern, replacement in metric_replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    # Older prompt habits sometimes put a Missing: line under every pick.
    cleaned = re.sub(r"(?im)^Missing:\s*.*(?:\n|$)", "", cleaned)
    # Free cards should stay clean. Reasons, value prose, and long disclaimers
    # are reserved for VIP/deeper cards or the inline Disclaimer tab.
    cleaned = re.sub(r"(?ims)^Reason:\s*\n?.*?(?=\n\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^Reason:\s*.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?ims)^📈 Value Note:\s*\n?.*?(?=\n\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^📈 Value Note:\s*.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?ims)^⚠️ BETGPTAI NOTE\s*.*?(?=\n\n━━━━━━━━━━━━|\Z)", "", cleaned)
    cleaned = re.sub(r"(?ims)^⚠️ BETGPTAI RECOMMENDATION\s*.*?(?=\n\n━━━━━━━━━━━━|\Z)", "", cleaned)
    cleaned = re.sub(r"(?im)^Educational analysis only.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^Singles are recommended.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^Parlays .*optional.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^Past performance.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^Card timing follows.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"(?im)^⏰ All game times.*(?:\n|$)", "", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


async def _analyze_mlb_slate_with_openai(
    slate: list[dict[str, Any]], api_key: str
) -> str:
    """Use the OpenAI Responses API to create the free Telegram card."""
    if not api_key:
        raise AIAnalysisError("OPENAI_API_KEY is missing from .env.")
    if not slate:
        return "No MLB games were found for today."

    # The model can be changed in .env without editing this Python file.
    model = os.getenv("OPENAI_MODEL", "gpt-5.5")
    instructions = (
        "You are a careful MLB slate analyst. Use only the supplied BETGPTAI "
        "v20.0 engine outputs plus the verified schedule, "
        "probable-pitcher stats, Baseball Savant/Statcast metrics, recent form, "
        "optional FanGraphs/pybaseball metrics, weather, park labels, Highlightly "
        "news/injuries/lineups/previews/team stats, and multi-book odds. Never "
        "invent statistics, lineups, injuries, bullpen "
        "quality, splits, trends, or other facts. Use available context naturally "
        "inside short reasons without producing deep game breakdowns. Do not "
        "list missing data and do not discuss every matchup. "
        "Compare all supplied books and use best_available_prices to select the "
        "best price internally. Never reveal a sportsbook or bookmaker name. "
        "Never use the words guaranteed, lock, 99.9%, or sure win. Do not "
        "include Reason lines in the free card; reasons are reserved for VIP. "
        "The engine calculates; you evaluate and explain. You may use "
        "betgptai_quant_v20 and betgptai_internal fields as hidden support, "
        "but never mention internal scoring, formulas, engines, provider names, raw "
        "Savant statistics, or model disagreements to members."
    )
    prompt = f"""
Analyze this MLB slate:

{json.dumps(_model_safe_slate(slate), indent=2)}

Follow this exact Telegram layout, including emojis, blank lines, and divider
lines. Use these section headings in this exact order:

🔥 PLAY OF THE DAY
🏆 TOP 5 MONEYLINE
🔥 TOP 5 F5
📈 TOP 5 RUN LINE
🎯 TOP 5 GAME TOTALS
💰 TOP 5 TEAM TOTALS
🧩 2-LEG SAFE PARLAY

PLAY OF THE DAY is the highest-confidence edge, not a guaranteed win. Choose it
only when the supplied data supports the strongest combination of home field,
starting pitcher matchup, recent form, weather/park context, matchup separation,
and market value. Use only whichever of those fields are actually supplied.

Format PLAY OF THE DAY like this:

⚾ [selection]
Risk Grade: [1-10]

Format each top-five pick like this, using 1️⃣ through 5️⃣:

1️⃣ [selection, including spread or total point when applicable]
Risk Grade: [1-10, where 10 means highest risk]

For F5, output up to five moneyline-only leans when available in this format
and no other F5 market:

🔥 TOP 5 F5

1️⃣ [Team] F5 ML
Risk Grade: [number]/10

2️⃣ [Team] F5 ML
Risk Grade: [number]/10

Never output an F5 total or F5 runline. Do not display a line or price in the
F5 section.

For every total, include its matchup in the selection so it can be tracked,
for example: "Under 8.5 (Dodgers at Giants)". Do not add matchups that are not
part of one of the two selected totals.

For TOP 5 TEAM TOTALS, use official team_total markets when available. If the
official team-total market is missing but a team-total side can be inferred from
the model edge, use the safe default display:
Team Total Over 4.5 / Safer Alt: Over 3.5
or
Team Total Under 5.5 / Safer Alt: Under 6.5
Only write "Team-total side unavailable from current feed." if no side can be
inferred.

When real team-total markets are available, output up to five picks in this exact
numbered format:

1️⃣ [Team] Team Total [Over/Under] [line]
Safer Alt: [Over one run lower / Under one run higher]
Risk Grade: [number]/10

2️⃣ [Team] Team Total [Over/Under] [line]
Safer Alt: [Over one run lower / Under one run higher]
Risk Grade: [number]/10

Put one blank line between picks and put {DIVIDER} between sections. Use all
bookmakers internally to find the best number, but NEVER display, mention, or
identify a sportsbook/bookmaker name. Do not display American odds or prices.
Show only the required market number inside the pick text, such as Runline -1.5,
Under 8.5 Runs, or Team Total Over 3.5.
Within every multi-pick section and the parlay, list the selected games in
chronological scheduled order. The application adds exact ET matchup/time lines.

Format the parlay as exactly two lines beginning with ✅. Do not include a
parlay note, disclaimer, or recommendation footer.

Use BETGPTAI v20.0 engine_decision as a hard gate. Only select official picks
from games marked QUALIFIED. If a game is PASS, do not use it as an official
pick. Score every MLB game behind the scenes before choosing the free card. Prioritize:
starting-pitcher edge (ERA, WHIP, K-BB %, xERA, xBA/xSLG allowed, Barrel %,
HardHit %, Whiff %, Chase %), team offense edge (OPS/xwOBA vs handedness, recent
scoring form, lineup strength), bullpen edge (ERA, WHIP, rest/fatigue, recent
usage, K-BB %, hard contact allowed), weather/park context (wind, temperature,
rain risk, hitter/pitcher environment), market value (implied probability,
model probability, edge percentage and avoiding bad prices), situational spot
(home field, travel, rest, getaway day, series context), and hidden AI
validation. If both internal analysts support the same side, boost the score
silently. If they disagree, lower the score silently.

Market rules: moneyline should be the safest winner profile; runline requires
team separation plus offensive advantage; totals require weather, SP contact
quality, bullpen fatigue, and scoring environment; team totals require weak
opposing SP/bullpen plus offense vs handedness; F5 is always moneyline-only and
uses starting pitcher plus early offense matchup. Moneyline -190 or worse is
PASS. Moneyline -165 to -189 should usually become run line, F5, team total, or
opponent +1.5 unless the engine output clearly supports otherwise. FanGraphs fields, when
available, are internal support for xFIP, SIERA, K-BB %, wRC+, wOBA, ISO, OPS,
Hard %, Pull %, WAR, and team pitching context. Keep each reason short and do
not list unavailable fields inside individual pick reasons.
The slate may include betgptai_quant_v20 and betgptai_internal scoring summaries. Use them only as
behind-the-scenes support. Never show raw model scores, source/provider names,
AI disagreements, engine names, formulas, or long model logic in Telegram.
Do not use "guaranteed", "lock", "99.9%", or "sure win". Do not create a list
merely to fill a quota: write "No additional qualified play" when evidence is
insufficient. Official markets are only Play of the Day, Top 5 Moneyline, Top 5
Run Line, Top 5 F5, Top 5 Game Totals, Top 5 Team Totals, and Safe 2-Leg Parlay.
Do not include F5 totals, F5 runlines, NRFI/YRFI sections, data limitations, extra matchups,
a full slate, a premium CTA, or a disclaimer. Stop immediately
after the two-leg parlay note; the application adds the footer itself.
""".strip()

    client = AsyncOpenAI(api_key=api_key)
    response = await client.responses.create(
        model=model,
        instructions=instructions,
        input=prompt,
    )

    analysis = response.output_text.strip()
    if not analysis:
        raise AIAnalysisError("OpenAI returned an empty analysis.")
    cleaned = _sanitize_telegram_output(analysis, slate)
    required_headings = (
        "🔥 PLAY OF THE DAY", "🏆 TOP 5 MONEYLINE",
        "🔥 TOP 5 F5",
        "📈 TOP 5 RUN LINE", "🎯 TOP 5 GAME TOTALS",
        "💰 TOP 5 TEAM TOTALS",
        "🧩 2-LEG SAFE PARLAY",
    )
    if any(heading not in cleaned for heading in required_headings):
        raise AIAnalysisError("OpenAI returned an incomplete Telegram card.")
    forbidden_content = (
        "DATA LIMITATIONS", "NRFI", "YRFI", "F5 TOTAL", "F5 RUNLINE",
        "WANT THE FULL BETGPTAI", "3-LEG",
    )
    if any(value in cleaned.upper() for value in forbidden_content):
        raise AIAnalysisError("OpenAI returned content outside the free card.")
    # The footer and disclaimer are controlled by code so they stay exact.
    cleaned = re.sub(
        r"Educational analysis only\.\s*Play responsibly\.",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    card = _sort_mlb_card_picks(
        f"{cleaned}\n\n{FREE_CARD_FOOTER}", slate
    )
    return _add_mlb_game_context(card, slate)


async def analyze_mlb_slate(
    slate: list[dict[str, Any]], api_key: str, anthropic_api_key: str = ""
) -> str:
    """Run both analysts independently and preserve an API-only final fallback."""
    slate = upcoming_mlb_slate(slate)
    if slate and not any(game.get("betgptai_quant_v20") for game in slate):
        try:
            slate = enrich_slate_with_quant_scores(slate)
        except Exception:
            # Quant scoring is deterministic but non-blocking; the API-backed
            # fallback card still works if one engine file has an issue.
            pass
    if not slate:
        return "No upcoming MLB games are available for today’s free card."

    # Keep the requested analyst order explicit: OpenAI writes the first card,
    # then Claude reviews the identical slate independently.
    try:
        openai_result: str | Exception = await _analyze_mlb_slate_with_openai(
            slate, api_key
        )
    except Exception as error:
        openai_result = error
    try:
        claude_result: dict[str, Any] | Exception = await analyze_slate_with_claude(
            _model_safe_slate(slate), anthropic_api_key, "mlb"
        )
    except Exception as error:
        claude_result = error
    if isinstance(openai_result, Exception):
        print(f"OpenAI Error:\n{openai_result}", file=sys.stderr, flush=True)
        traceback.print_exception(
            type(openai_result), openai_result, openai_result.__traceback__,
            file=sys.stderr,
        )
        openai_card: str | None = None
    else:
        openai_card = openai_result
    if isinstance(claude_result, Exception):
        print(f"Claude Error:\n{claude_result}", file=sys.stderr, flush=True)
        traceback.print_exception(
            type(claude_result), claude_result, claude_result.__traceback__,
            file=sys.stderr,
        )
        claude_data: dict[str, Any] | None = None
    else:
        claude_data = _validate_claude_mlb(claude_result, slate)

    # If one analyst is unavailable, the other still owns the card. Only when
    # both fail do we fall all the way back to schedule + market data.
    if openai_card is not None:
        card = openai_card
    elif claude_data is not None:
        card = _build_claude_mlb_card(claude_data, slate)
    else:
        _remember_analysis_metadata(False, False, False, True, slate)
        return _apply_public_confidence(
            build_fallback_card(slate), 5, "No major value edge detected."
        )

    openai_pick = _primary_pick_from_card(openai_card) if openai_card else None
    claude_pick = claude_data.get("play_of_day") if claude_data else None
    internal_value = value_context_for_pick(slate, openai_pick or claude_pick)
    if internal_value.get("verified_positive_ev") and (openai_pick or claude_pick):
        target_pick = openai_pick or claude_pick
        target_pick["implied_probability"] = internal_value.get("implied_probability")
        target_pick["estimated_probability"] = internal_value.get("projected_probability")
    evidence = evidence_for_pick(slate, openai_pick or claude_pick, "mlb")
    confidence = public_confidence_summary(openai_pick, claude_pick, evidence)
    _remember_analysis_metadata(
        openai_card is not None,
        claude_data is not None,
        bool(confidence.get("agreement")),
        False,
        slate,
    )
    return _apply_public_confidence(
        card, confidence["grade"], confidence["value_note"]
    )


def _primary_pick_from_card(card: str | None) -> dict[str, Any] | None:
    """Read OpenAI's featured selection back from its Telegram card."""
    if not card:
        return None
    match = re.search(
        r"🔥 PLAY OF THE DAY\s+⚾\s*(.+?)\n(?:🆚.*\n🕒.*\n)?",
        card,
        flags=re.S,
    )
    if not match:
        return None
    selection = match.group(1).strip()
    if re.search(r"(?i)\bteam total\b", selection):
        market = "team_total"
    elif re.search(r"(?i)\bF5\b", selection):
        market = "f5_moneyline"
    elif re.match(r"(?i)^(over|under)", selection):
        market = "totals"
    elif re.search(r"[+-]\d+(?:\.\d+)?", selection):
        market = "runline"
    else:
        market = "moneyline"
    return {
        "selection": selection,
        "market": market,
        "line": None,
        "implied_probability": None,
        "estimated_probability": None,
    }


def _validate_claude_mlb(
    data: dict[str, Any], slate: list[dict[str, Any]]
) -> dict[str, Any]:
    """Remove invented Claude prices and event IDs before Telegram rendering."""
    game_ids = {str(game.get("game_id")) for game in slate}
    allowed_prices = {
        float(price["price"])
        for game in slate for price in game.get("best_available_prices", [])
        if isinstance(price.get("price"), (int, float))
    }

    def validate(pick: Any) -> dict[str, Any] | None:
        if not isinstance(pick, dict):
            return None
        game_id = pick.get("game_id")
        if game_id is not None and str(game_id) not in game_ids:
            return None
        cleaned = dict(pick)
        line = cleaned.get("line")
        if isinstance(line, (int, float)) and float(line) not in allowed_prices:
            cleaned["line"] = None
            cleaned["implied_probability"] = None
        elif isinstance(line, (int, float)):
            cleaned["implied_probability"] = _implied_probability(line)
        return cleaned

    validated: dict[str, Any] = {}
    for key, value in data.items():
        if key == "play_of_day":
            validated[key] = validate(value)
        elif isinstance(value, list):
            validated[key] = [pick for item in value if (pick := validate(item))]
        else:
            validated[key] = value
    return validated


def _claude_pick_text(pick: dict[str, Any], prefix: str = "⚾ ") -> str:
    """Format one structured Claude pick without adding unsupported claims."""
    grade = pick.get("risk_grade")
    grade_text = f"{grade:g}/10" if isinstance(grade, (int, float)) else "Unavailable"
    reason = str(pick.get("reason") or "Available slate data supports this angle.")
    reason = re.sub(r"(?i)\b(?:guaranteed|lock|sure win|99\.9%)\b", "high-confidence edge", reason)
    return (
        f"{prefix}{pick.get('selection', 'No qualified play')}\n"
        f"Risk Grade: {grade_text}\nReason: {reason}"
    )


def _claude_list(data: dict[str, Any], key: str, limit: int = 5) -> str:
    picks = data.get(key, [])
    if not isinstance(picks, list) or not picks:
        return "No additional qualified play."
    return "\n\n".join(
        _claude_pick_text(pick, f"{NUMBER_EMOJIS[index]} ")
        for index, pick in enumerate(picks[:limit])
    )


def _claude_f5_list(data: dict[str, Any], limit: int = 5) -> str:
    """Render Claude F5 leans as moneyline-only picks with no displayed price."""
    picks = data.get("f5_moneyline", [])
    if not isinstance(picks, list) or not picks:
        return "No qualified F5 moneyline lean is available."
    lines: list[str] = []
    for index, pick in enumerate(picks[:limit]):
        if not isinstance(pick, dict):
            continue
        grade = pick.get("risk_grade")
        grade_text = f"{grade:g}/10" if isinstance(grade, (int, float)) else "Unavailable"
        selection = str(pick.get("selection") or "No qualified F5 moneyline lean")
        if "F5" not in selection.upper():
            selection = f"{selection} F5 ML"
        reason = str(pick.get("reason") or "Starting matchup supports the early-game lean.")
        reason = re.sub(r"(?i)\b(?:guaranteed|lock|sure win|99\.9%)\b", "high-confidence edge", reason)
        lines.append(
            f"{NUMBER_EMOJIS[index]} {selection}\n"
            f"Risk Grade: {grade_text}\n"
            f"Reason: {reason}"
        )
    return "\n\n".join(lines) if lines else "No qualified F5 moneyline lean is available."


def _claude_team_total_list(
    data: dict[str, Any], slate: list[dict[str, Any]] | None = None, limit: int = 5
) -> str:
    """Render team totals with safer one-run alternates when Claude has them."""
    picks = data.get("team_totals", [])
    if not isinstance(picks, list) or not picks:
        return _format_team_total_list(slate or [], limit)
    rendered: list[str] = []
    for index, pick in enumerate(picks[:limit]):
        if not isinstance(pick, dict):
            continue
        text = _claude_pick_text(pick, f"{NUMBER_EMOJIS[index]} ")
        selection = str(pick.get("selection") or "")
        safer_line = ""
        match = re.search(r"\b(Over|Under)\s+(\d+(?:\.\d+)?)\b", selection, flags=re.I)
        if match:
            direction = match.group(1).title()
            point = float(match.group(2))
            safer = point - 1 if direction == "Over" else point + 1
            safer_line = f"\nSafer Alt: {direction} {safer:g}"
        if safer_line and "\nLine:" in text:
            text = text.replace("\nLine:", f"{safer_line}\nLine:", 1)
        elif safer_line:
            text = text.replace("\nRisk Grade:", f"{safer_line}\nRisk Grade:", 1)
        rendered.append(text)
    return "\n\n".join(rendered) if rendered else _format_team_total_list(slate or [], limit)


def _build_claude_mlb_card(data: dict[str, Any], slate: list[dict[str, Any]]) -> str:
    """Render a free MLB card when OpenAI is unavailable but Claude succeeds."""
    play = data.get("play_of_day")
    featured = _claude_pick_text(play) if isinstance(play, dict) else "No qualified play."
    parlay = data.get("safe_parlay", [])
    parlay_text = (
        "\n".join(f"✅ {pick.get('selection')}" for pick in parlay[:2])
        if isinstance(parlay, list) and len(parlay) >= 2
        else "No qualified two-leg parlay."
    )
    card = f"""🔥 PLAY OF THE DAY

{featured}

{DIVIDER}

🏆 TOP 5 MONEYLINE

{_claude_list(data, 'moneyline')}

{DIVIDER}

🔥 TOP 5 F5

{_claude_f5_list(data)}

{DIVIDER}

📈 TOP 5 RUN LINE

{_claude_list(data, 'runline')}

{DIVIDER}

🎯 TOP 5 GAME TOTALS

{_claude_list(data, 'totals')}

{DIVIDER}

💰 TOP 5 TEAM TOTALS

{_claude_team_total_list(data, slate)}

{DIVIDER}

🧩 2-LEG SAFE PARLAY

{parlay_text}

{FREE_CARD_FOOTER}"""
    cleaned = _sanitize_telegram_output(card, slate)
    return _add_mlb_game_context(_sort_mlb_card_picks(cleaned, slate), slate)


def _insert_consensus(card: str, consensus: str) -> str:
    """Place consensus above the premium CTA while preserving the shared footer."""
    marker = f"{DIVIDER}\n\n💎 WANT THE FULL BETGPTAI PREMIUM CARD?"
    replacement = f"{DIVIDER}\n\n{consensus}\n\n{marker}"
    return card.replace(marker, replacement, 1) if marker in card else f"{card}\n\n{DIVIDER}\n\n{consensus}"
