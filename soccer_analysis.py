"""Create relaxed public and detailed owner soccer cards with OpenAI."""

from __future__ import annotations

import json
import asyncio
import os
import re
import sys
import traceback
from typing import Any

from openai import AsyncOpenAI

from card_format import ODDS_SHOPPING_FOOTER, TIMED_CARD_FOOTER
from card_time import CARD_TIMING_FOOTER
from game_time import GAME_TIME_FOOTER, game_sort_key, soccer_game_block
from consensus_analysis import (
    analyze_slate_with_claude,
    evidence_for_pick,
    public_confidence_summary,
)


DIVIDER = "━━━━━━━━━━━━"
PUBLIC_MARKETS = {
    "double_chance", "over_1_5", "under_3_5", "btts", "draw_no_bet", "moneyline"
}
TOURNAMENT_KEYWORDS = (
    "world cup", "fifa world cup", "uefa euro", "euro ",
    "copa america", "gold cup",
)


def _clean(text: str) -> str:
    """Remove prohibited certainty wording from model-written explanations."""
    cleaned = str(text).strip()
    cleaned = re.sub(r"(?im)^Line:\s*[+-]?\d+(?:\.\d+)?(?:\s+.*)?\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^Odds:\s*[+-]?\d+(?:\.\d+)?(?:\s+.*)?\n?", "", cleaned)
    cleaned = re.sub(r"(?im)^Best(?: available)? odds.*\n?", "", cleaned)
    cleaned = re.sub(r"(?i)\bguaranteed\b", "high-confidence", cleaned)
    cleaned = re.sub(r"(?i)\bsure win\b", "stronger lean", cleaned)
    cleaned = re.sub(r"(?i)\block\b", "play", cleaned)
    cleaned = re.sub(r"(?i)99\.9\s*%", "high confidence", cleaned)
    cleaned = re.sub(r"(?i)\+EV", "model-supported value", cleaned)
    for pattern in (
        r"\bOpenAI\b", r"\bClaude\b", r"\bAnthropic\b", r"\bFootball-Data\.org\b",
        r"\bTheSportsDB\b", r"\bOdds API\b", r"\bThe Odds API\b",
        r"\bOpen-Meteo\b", r"\bClubElo\b", r"\bUnderstat\b", r"\bSerpApi\b",
        r"\bStatsBomb\b", r"\bFBref\b", r"\bAPI-Football\b", r"\bAPI Sports\b",
    ):
        cleaned = re.sub(pattern, "available data", cleaned, flags=re.I)
    for pattern, replacement in (
        (r"\bxG\b", "attacking profile"),
        (r"\bxGA\b", "defensive profile"),
        (r"\bElo\b", "team-strength profile"),
    ):
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.I)
    return cleaned


def _competition_flag(code: Any) -> str:
    """Convert common Football-Data.org area codes into flag emoji."""
    alpha_two = {
        "ENG": "GB", "ESP": "ES", "DEU": "DE", "ITA": "IT",
        "FRA": "FR", "NLD": "NL", "PRT": "PT", "USA": "US",
        "BRA": "BR", "ARG": "AR", "MEX": "MX",
    }.get(str(code or "").upper())
    if not alpha_two:
        return "🌍"
    return "".join(chr(127397 + ord(character)) for character in alpha_two)


def _market_label(market: str, game: dict[str, Any], short: bool = False) -> str:
    if market == "btts":
        return "BTTS" if short else "BTTS — Yes"
    if market == "over_1_5":
        return "Over 1.5 Goals"
    if market == "under_3_5":
        return "Under 3.5 Goals"
    if market == "double_chance":
        return "Double Chance" if short else f"{game['home_team']} or Draw"
    if market == "moneyline":
        return game["home_team"] if short else f"{game['home_team']} Moneyline"
    return "Draw No Bet" if short else f"{game['home_team']} Draw No Bet"


def _is_tournament_match(game: dict[str, Any]) -> bool:
    """Detect major international tournaments without requiring one provider."""
    haystack = " ".join(
        str(game.get(key, ""))
        for key in ("competition", "stage", "group", "round")
    ).lower()
    context = game.get("world_cup_context")
    if isinstance(context, dict):
        haystack += " " + " ".join(str(value) for value in context.values()).lower()
    return any(keyword in haystack for keyword in TOURNAMENT_KEYWORDS)


def _tournament_active(slate: list[dict[str, Any]]) -> bool:
    """World Cup mode activates only while tournament matches are scheduled."""
    return any(_is_tournament_match(game) for game in slate)


def _league_key(game: dict[str, Any]) -> str:
    return str(game.get("competition") or game.get("league") or "Soccer")


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _american_line(value: Any) -> str:
    """Format American odds without exposing a sportsbook source."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "Not available"
    return f"+{value:g}" if value > 0 else f"{value:g}"


def _implied_probability(price: int | float) -> float:
    return abs(price) / (abs(price) + 100) if price < 0 else 100 / (price + 100)


def _market_price(game: dict[str, Any], market: str) -> dict[str, Any] | None:
    """Find an exact supplied price for one candidate market."""
    prices = [
        price for price in game.get("best_available_prices", [])
        if isinstance(price, dict) and isinstance(price.get("price"), (int, float))
    ]
    if market == "btts":
        matches = [price for price in prices if price.get("market") in {"btts", "both_teams_to_score"} and str(price.get("outcome", "")).lower() in {"yes", "btts yes"}]
    elif market == "over_1_5":
        matches = [price for price in prices if price.get("market") == "totals" and str(price.get("outcome", "")).lower() == "over" and _number(price.get("point")) == 1.5]
    elif market == "under_3_5":
        matches = [price for price in prices if price.get("market") == "totals" and str(price.get("outcome", "")).lower() == "under" and _number(price.get("point")) == 3.5]
    elif market == "double_chance":
        matches = [price for price in prices if price.get("market") in {"double_chance", "doublechance"}]
    elif market == "draw_no_bet":
        matches = [price for price in prices if price.get("market") in {"draw_no_bet", "dnb"}]
    else:
        matches = [price for price in prices if price.get("market") == "h2h" and str(price.get("outcome", "")).lower() != "draw"]
        return max(matches, key=lambda item: _implied_probability(item["price"])) if matches else None
    return max(matches, key=lambda item: item["price"]) if matches else None


def _positive_recent_form(game: dict[str, Any]) -> bool:
    for key in ("home_recent", "away_recent"):
        form = game.get(key)
        if isinstance(form, dict):
            wins, losses = _number(form.get("wins")), _number(form.get("losses"))
            if wins is not None and losses is not None and wins > losses:
                return True
    return False


def _risk_grade(score: int) -> int:
    if score >= 80:
        return 5
    if score >= 65:
        return 6
    if score >= 50:
        return 7
    return 8


def _heuristic_public_choices(slate: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score all markets 1–100 and always select two when fixtures exist."""
    priority_order = {
        "double_chance": 0, "over_1_5": 1, "under_3_5": 2,
        "btts": 3, "draw_no_bet": 4, "moneyline": 5,
    }
    availability_bonus = {
        "double_chance": 18, "draw_no_bet": 18, "over_1_5": 16,
        "btts": 15, "moneyline": 14, "under_3_5": 12,
    }
    candidates = []
    for game in slate:
        positive_form = _positive_recent_form(game)
        for market in priority_order:
            price = _market_price(game, market)
            # A scheduled fixture begins at a neutral baseline. Sparse data
            # never rejects the play; it only lowers confidence.
            score = 52
            reasons = []
            if price is not None:
                score += availability_bonus[market]
                score += 10
                reasons.append("available odds value")
            home_side = market == "double_chance"
            if market == "moneyline" and price is not None:
                home_side = price.get("outcome") == game.get("home_team")
            if home_side:
                score += 10
                reasons.append("home-field context")
            if positive_form:
                score += 10
                reasons.append("positive recent form")
            internal_scores = (
                game.get("soccer_internal", {}).get("market_scores", {})
                if isinstance(game.get("soccer_internal"), dict)
                else {}
            )
            internal_score = internal_scores.get(market)
            if isinstance(internal_score, (int, float)):
                # Hidden Soccer Master System engines can lift or lower a play,
                # but the public reason remains simple and premium.
                score = round((score * 0.55) + (internal_score * 0.45))
                if internal_score >= 65:
                    reasons.append("model-supported matchup profile")
            score = max(1, min(100, score))
            if market == "double_chance":
                base_reason = "Safer side profile using match setup and team context."
            elif market in {"over_1_5", "btts"}:
                base_reason = "Attacking profile supports a goals-based lean."
            elif market == "under_3_5":
                base_reason = "Game profile supports a conservative total lean."
            else:
                base_reason = "Matchup profile supports the stronger side."
            reason = base_reason if not reasons else f"{base_reason} Added support from {', '.join(reasons)}."
            candidates.append({
                "game": game,
                "market": market,
                "score": score,
                "risk_grade": _risk_grade(score),
                "line": price.get("price") if price else None,
                "reason": reason,
            })
    candidates.sort(
        # Market priority is the first decision rule; the evidence score ranks
        # competing fixtures within that market.
        key=lambda choice: (priority_order[choice["market"]], -choice["score"])
    )
    selected = []
    used = set()
    used_markets = set()
    league_counts: dict[str, int] = {}
    for choice in candidates:
        key = (choice["game"].get("match_id"), choice["market"])
        league = _league_key(choice["game"])
        if key in used or choice["market"] in used_markets or league_counts.get(league, 0) >= 2:
            continue
        used.add(key)
        used_markets.add(choice["market"])
        league_counts[league] = league_counts.get(league, 0) + 1
        selected.append(choice)
        if len(selected) == 2:
            break
    return selected


def _choices_for_game(game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create primary and secondary tournament picks for a single match."""
    candidates = [
        choice for choice in _heuristic_public_choices([game])
        if choice.get("game") is game
    ]
    if not candidates:
        candidates = [
            {
                "game": game,
                "market": "double_chance",
                "score": 58,
                "risk_grade": 7,
                "line": None,
                "reason": "Safer side profile with match-context support.",
            },
            {
                "game": game,
                "market": "over_1_5",
                "score": 55,
                "risk_grade": 7,
                "line": None,
                "reason": "Conservative goals angle fits the matchup profile.",
            },
        ]
    if len(candidates) == 1:
        fallback_market = "over_1_5" if candidates[0]["market"] != "over_1_5" else "under_3_5"
        candidates.append({
            "game": game,
            "market": fallback_market,
            "score": max(50, int(candidates[0].get("score", 58)) - 4),
            "risk_grade": 7,
            "line": None,
            "reason": "Secondary angle uses the safest available market profile.",
        })
    return candidates[0], candidates[1]


def _confidence_from_choice(choice: dict[str, Any]) -> int:
    """Convert hidden score into the public 5-8 confidence scale."""
    score = int(choice.get("score") or 55)
    if score >= 80:
        return 8
    if score >= 65:
        return 7
    if score >= 50:
        return 6
    return 5


def _tournament_card(slate: list[dict[str, Any]]) -> str:
    """World Cup Mode: show every scheduled international tournament match."""
    games = sorted(slate, key=lambda game: game_sort_key(game, "kickoff"))
    sections = ["⚽ BETGPTAI SOCCER CARD\n\n🌍 WORLD CUP MODE"]
    for game in games:
        primary, secondary = _choices_for_game(game)
        match_line = f"{game.get('home_team', 'Home Team')} vs {game.get('away_team', 'Away Team')}"
        game_time = game.get("game_time_et")
        if not game_time:
            block = soccer_game_block(game).splitlines()
            game_time = block[1].replace("🕒 ", "") if len(block) > 1 else "Time unavailable ET"
        sections.append(
            f"{DIVIDER}\n\n"
            "Match:\n"
            f"{match_line}\n\n"
            "Game Time ET:\n"
            f"{game_time}\n\n"
            "Primary Pick:\n"
            f"{_market_label(primary['market'], game)}\n\n"
            "Secondary Pick:\n"
            f"{_market_label(secondary['market'], game)}\n\n"
            f"Confidence Grade: {_confidence_from_choice(primary)}/10\n\n"
            "Short Reason:\n"
            f"{_clean(primary.get('reason') or 'Safer matchup profile with market-priority support.')}"
        )
    sections.append(
        f"{DIVIDER}\n\n"
        f"{ODDS_SHOPPING_FOOTER}\n\n"
        "Educational analysis only. Play responsibly.\n\n"
        f"{CARD_TIMING_FOOTER}\n\n"
        f"{GAME_TIME_FOOTER}"
    )
    return "\n\n".join(sections)


def _public_limitations(choices: list[dict[str, Any]]) -> str:
    """List missing context once for the selected public fixtures."""
    games = [choice["game"] for choice in choices if isinstance(choice.get("game"), dict)]
    missing = []
    # Public members should not see a long missing-data audit. Only mention the
    # one limitation that materially affects the visible card: absent prices.
    if games and all(not game.get("best_available_prices") for game in games):
        missing.append("Odds unavailable.")
    elif any(choice.get("line") is None for choice in choices):
        missing.append("Some selected market lines are unavailable.")
    return "📋 DATA LIMITATIONS\n\n" + "\n".join(missing) if missing else ""


def _public_card(
    choices: list[dict[str, Any]], confidence: dict[str, Any] | None = None
) -> str:
    """Build the exact relaxed /soccer preview."""
    if len(choices) < 2:
        return "No qualified soccer plays right now."
    featured = choices[0]
    game = featured["game"]
    def selection(choice: dict[str, Any], short: bool = False) -> str:
        choice_game = choice["game"]
        return _market_label(choice["market"], choice_game, short=short)

    def confidence_grade(choice: dict[str, Any]) -> int:
        score = int(choice.get("score") or 60)
        if score >= 80:
            return 8
        if score >= 65:
            return 7
        if score >= 50:
            return 6
        return 5

    def top_pick(index: int, choice: dict[str, Any]) -> str:
        block = soccer_game_block(choice["game"]).splitlines()
        match_line = block[0].replace("🆚 ", "")
        time_line = block[1] if len(block) > 1 else "🕒 Time unavailable ET"
        return (
            f"{index}️⃣ Pick: {selection(choice)}\n"
            f"🆚 Match: {match_line}\n"
            f"{time_line.replace('🕒 ', '🕒 Game Time ET: ')}\n"
            f"Confidence Grade: {confidence_grade(choice)}/10\n"
            f"Reason: {_clean(choice['reason']).replace(chr(10), ' ')}"
        )
    chronological = sorted(
        choices, key=lambda choice: game_sort_key(choice["game"], "kickoff")
    )
    confidence = confidence or {"grade": confidence_grade(featured), "value_note": "No major value edge detected."}
    featured_block = soccer_game_block(game).splitlines()
    featured_match = featured_block[0].replace("🆚 ", "") if featured_block else "Match unavailable"
    featured_time = featured_block[1] if len(featured_block) > 1 else "🕒 Time unavailable ET"
    return (
        "⚽ BETGPTAI SOCCER CARD\n\n"
        f"{DIVIDER}\n\n"
        "🔥 PLAY OF THE DAY\n\n"
        f"Pick: {selection(featured)}\n"
        f"🆚 Match: {featured_match}\n"
        f"{featured_time.replace('🕒 ', '🕒 Game Time ET: ')}\n"
        f"Confidence Grade: {confidence_grade(featured)}/10\n"
        f"Reason: {_clean(featured['reason']).replace(chr(10), ' ')}\n\n"
        f"{DIVIDER}\n\n"
        "🏆 TOP 2 SOCCER PLAYS\n\n"
        f"{top_pick(1, chronological[0])}\n\n"
        f"{top_pick(2, chronological[1])}\n\n"
        f"{DIVIDER}\n\n"
        "🧩 SAFE PARLAY OF THE DAY\n\n"
        f"✅ {selection(chronological[0], short=True)}\n"
        f"✅ {selection(chronological[1], short=True)}\n"
        "\n\n"
        f"{DIVIDER}\n\n"
        "⚠️ Singles are recommended for better long-term results.\n"
        "Parlays are optional and higher risk.\n\n"
        f"{ODDS_SHOPPING_FOOTER}\n\n"
        "Educational analysis only. Play responsibly.\n\n"
        f"{CARD_TIMING_FOOTER}\n\n"
        f"{GAME_TIME_FOOTER}"
    )


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("OpenAI did not return a soccer JSON object.")
    return payload


async def _public_ai_choices(
    slate: list[dict[str, Any]], api_key: str
) -> list[dict[str, Any]]:
    """Rank two flexible markets without requiring a complete data profile."""
    prompt = f"""
Select two soccer plays from these scheduled matches:
{json.dumps(slate, indent=2)}

Prioritize markets in this exact order: double_chance, over_1_5,
under_3_5, btts, draw_no_bet, moneyline. Use any available match information, odds, recent form,
splits, goals, league environment, motivation, H2H, corners, or weather. Do not
require every factor or an odds price. Never invent a statistic.
Use soccer_internal fields only as hidden support. Never mention engines,
formulas, provider names, raw statistics, model disagreement, or API/source
availability in public reasons.
StatsBomb and FBref context, when supplied, is also hidden support only.
API-Football context, when supplied, is hidden support only.

Return JSON only: {{"plays":[{{"match_id":1,"market":"double_chance",
"risk_grade":7,"reason":"short reason using available context",
"estimated_probability":0.56}}]}}. Return
exactly two choices. They may be different markets from the same match. Risk
grades must be 5 through 8. Do not mention missing context inside a pick reason;
the application adds one DATA LIMITATIONS section.
""".strip()
    client = AsyncOpenAI(api_key=api_key)
    response = await client.responses.create(
        model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
        instructions="You are a careful soccer analyst. Output valid JSON only.",
        input=prompt,
    )
    decision = _parse_json(response.output_text)
    selections = []
    used = set()
    used_markets = set()
    for choice in decision.get("plays", []):
        game = next(
            (item for item in slate if item.get("match_id") == choice.get("match_id")),
            None,
        )
        market = choice.get("market")
        key = (game.get("match_id"), market) if game else None
        grade = choice.get("risk_grade")
        if (
            not game
            or market not in PUBLIC_MARKETS
            or key in used
            or market in used_markets
            or not isinstance(grade, (int, float))
            or not 5 <= grade <= 8
        ):
            continue
        used.add(key)
        used_markets.add(market)
        selections.append({
            "game": game,
            "market": market,
            "risk_grade": grade,
            "line": (_market_price(game, market) or {}).get("price"),
            "estimated_probability": choice.get("estimated_probability"),
            "reason": str(
                choice.get("reason")
                or "Available match information supports this angle."
            ),
        })
    return selections


def _limitations(slate: list[dict[str, Any]]) -> str:
    """Create one consolidated limitations section for owner cards."""
    missing = []
    if all(game.get("h2h_history") == "unavailable" for game in slate):
        missing.append("H2H history is unavailable from the current free feed.")
    if all(game.get("corners_profile") == "unavailable" for game in slate):
        missing.append("Corner profiles and corner markets are unavailable.")
    if all(game.get("weather") == "unavailable" for game in slate):
        missing.append("Weather is unavailable for the covered fixtures.")
    return "DATA LIMITATIONS\n" + "\n".join(f"- {item}" for item in missing) if missing else ""


async def _owner_card(
    slate: list[dict[str, Any]], api_key: str, mode: str
) -> str:
    """Generate detailed protected views while retaining graceful fallback text."""
    rules = {
        "full": "Return up to five strongest plays across supported markets.",
        "btts": "Return up to three BTTS leans.",
        "overs": "Return up to three over/under goal leans.",
        "corners": "Return up to three corner market leans when available.",
        "cards": "Return up to three card market leans when available.",
        "first_half": "Return up to three first-half soccer leans only.",
        "second_half": "Return up to three second-half soccer leans only.",
        "double_chance": "Return up to three Double Chance or Draw No Bet leans only.",
        "asian_handicap": "Return up to three Asian Handicap leans when available.",
    }
    if not api_key:
        analysis = "OpenAI analysis unavailable; use /soccer for the data-based free card."
    else:
        prompt = f"""
Analyze this soccer slate with recent form, home/away splits, goals, BTTS and
over trends, H2H, league environment, motivation, corners, odds, and weather
when supplied. Missing factors must not block a pick or be repeated under picks.
{rules.get(mode, rules['full'])}

{json.dumps(slate, indent=2)}

For every play include Match, Market, Confidence Grade, and one short reason.
Do not display American odds or sportsbook names. Never display
data-provider/API source names or invent missing data.
""".strip()
        client = AsyncOpenAI(api_key=api_key)
        response = await client.responses.create(
            model=os.getenv("OPENAI_MODEL", "gpt-5.5"),
            instructions="You are a careful soccer analyst. Keep Telegram output concise.",
            input=prompt,
        )
        analysis = _clean(response.output_text)
    limitations = _limitations(slate)
    limitation_block = f"\n\n{DIVIDER}\n\n{limitations}" if limitations else ""
    return (
        f"💎 BETGPTAI SOCCER — {mode.upper()}\n\n{analysis}"
        f"{limitation_block}\n\n{TIMED_CARD_FOOTER}"
    )


async def analyze_soccer_slate(
    slate: list[dict[str, Any]], api_key: str, mode: str = "public",
    anthropic_api_key: str = "",
) -> str:
    """Always return a public card when matches exist; isolate optional AI errors."""
    if not slate:
        return "No qualified soccer plays right now."
    if mode == "public":
        if _tournament_active(slate):
            return _tournament_card(slate)
        heuristic = _heuristic_public_choices(slate)
        openai_result, claude_result = await asyncio.gather(
            _public_ai_choices(slate, api_key),
            analyze_slate_with_claude(slate, anthropic_api_key, "soccer"),
            return_exceptions=True,
        )
        if isinstance(openai_result, Exception):
            print(f"OpenAI Error:\n{openai_result}", file=sys.stderr, flush=True)
            primary_choices: list[dict[str, Any]] = []
        else:
            primary_choices = openai_result
        if isinstance(claude_result, Exception):
            print(f"Claude Error:\n{claude_result}", file=sys.stderr, flush=True)
            claude_choices: list[dict[str, Any]] = []
        else:
            claude_choices = _claude_soccer_choices(claude_result, slate)

        # OpenAI remains primary. Claude becomes the card owner only when the
        # primary analyst fails; deterministic choices fill any missing slot.
        displayed = primary_choices[:2] or claude_choices[:2] or heuristic[:2]
        for choice in heuristic:
            if len(displayed) >= 2:
                break
            identity = (choice["game"].get("match_id"), choice["market"])
            if all(
                (item["game"].get("match_id"), item["market"]) != identity
                for item in displayed
            ):
                displayed.append(choice)

        primary_pick = _soccer_consensus_pick(primary_choices[0]) if primary_choices else None
        claude_pick = _soccer_consensus_pick(claude_choices[0]) if claude_choices else None
        if primary_pick or claude_pick:
            evidence = evidence_for_pick(
                slate, primary_pick or claude_pick, "soccer"
            )
            confidence = public_confidence_summary(primary_pick, claude_pick, evidence)
        else:
            # Both model calls failed, so this remains a pure API-data fallback.
            confidence = None
        return _public_card(displayed, confidence)
    try:
        return await _owner_card(slate, api_key, mode)
    except Exception as error:
        print(f"OpenAI Error:\n{error}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return await _owner_card(slate, "", mode)


def _canonical_soccer_market(value: Any) -> str | None:
    text = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    aliases = {
        "btts_yes": "btts", "both_teams_to_score": "btts",
        "over_1_5_goals": "over_1_5", "over_1_5": "over_1_5",
        "under_3_5_goals": "under_3_5", "under_3_5": "under_3_5",
        "over_2_5_goals": "over_2_5", "over_2_5": "over_2_5",
        "1x2": "moneyline", "h2h": "moneyline", "ml": "moneyline",
        "dnb": "draw_no_bet", "draw_no_bet": "draw_no_bet",
        "double_chance": "double_chance", "moneyline": "moneyline",
        "btts": "btts",
    }
    return aliases.get(text)


def soccer_debug_report(
    slate: list[dict[str, Any]], debug_context: dict[str, Any] | None = None
) -> str:
    """Owner-only explanation of /soccer source and candidate behavior."""
    summary = debug_context or {}
    for game in slate:
        if isinstance(game.get("soccer_slate_summary"), dict):
            summary = {**game["soccer_slate_summary"], **summary}
        if isinstance(game.get("soccer_debug_context"), dict):
            summary = {**summary, **game["soccer_debug_context"]}
            break
    choices = _heuristic_public_choices(slate) if slate else []
    odds_markets = int(summary.get("odds_markets_found") or sum(
        len(game.get("best_available_prices", []))
        for game in slate
        if isinstance(game, dict)
    ))
    rejected = []
    if not slate:
        rejected.append("Zero scheduled matches across all enabled sources.")
    else:
        rejected.append(
            "No candidates rejected for missing xG, H2H, corners, injuries, odds, or API-Football."
        )
        if odds_markets == 0:
            rejected.append("Odds unavailable; fallback logic still creates leans.")
        if len(choices) < 2:
            rejected.append("Fewer than two candidate plays created from the available slate.")
    selected = [
        f"{_market_label(choice['market'], choice['game'])} — "
        f"{choice['game'].get('home_team')} vs {choice['game'].get('away_team')}"
        for choice in choices[:2]
    ]
    selected_text = "\n".join(f"- {item}" for item in selected) if selected else "None"
    rejected.extend(str(item) for item in summary.get("candidate_rejections", []) if item)
    rejected_text = "\n".join(f"- {item}" for item in dict.fromkeys(rejected)) if rejected else "None"
    return (
        "🧪 BETGPTAI SOCCER DEBUG\n\n"
        f"Football-Data matches: {summary.get('football_data_matches', summary.get('football_data_games', 0))}\n"
        f"TheSportsDB matches: {summary.get('thesportsdb_matches', summary.get('thesportsdb_games', 0))}\n"
        f"Fallback matches: {summary.get('world_cup_fallback_matches', 0)}\n"
        f"World Cup fallback matches: {summary.get('world_cup_fallback_matches', 0)}\n"
        f"StatsBomb enriched matches: {summary.get('statsbomb_games', 0)}\n"
        f"Weather matches: {summary.get('weather_games', 0)}\n"
        f"Filtered matches: {summary.get('matches_after_filter', len(slate))}\n"
        f"Matches after filtering: {summary.get('matches_after_filter', len(slate))}\n"
        f"Odds matches: {odds_markets}\n"
        f"Odds markets found: {odds_markets}\n"
        f"Qualified plays: {len(choices)}\n"
        f"Candidate plays created: {len(choices)}\n\n"
        "Why candidates were rejected:\n"
        f"{rejected_text}\n\n"
        "Final selected plays:\n"
        f"{selected_text}"
    )


def _claude_soccer_choices(
    data: dict[str, Any], slate: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Validate Claude soccer picks against real matches and market prices."""
    raw = []
    if isinstance(data.get("play_of_day"), dict):
        raw.append(data["play_of_day"])
    raw.extend(data.get("soccer_plays", []) if isinstance(data.get("soccer_plays"), list) else [])
    choices = []
    seen = set()
    for pick in raw:
        match_id = pick.get("match_id")
        game = next(
            (item for item in slate if str(item.get("match_id")) == str(match_id)),
            None,
        )
        market = _canonical_soccer_market(pick.get("market"))
        if not game or market not in PUBLIC_MARKETS:
            continue
        identity = (game.get("match_id"), market)
        if identity in seen:
            continue
        seen.add(identity)
        price = _market_price(game, market)
        grade = pick.get("risk_grade")
        choices.append({
            "game": game,
            "market": market,
            "line": price.get("price") if price else None,
            "risk_grade": max(5, min(8, grade)) if isinstance(grade, (int, float)) else 8,
            "reason": str(pick.get("reason") or "Available match data supports this angle."),
            "estimated_probability": pick.get("estimated_probability"),
            "implied_probability": _implied_probability(price.get("price")) if price else None,
        })
    return choices


def _soccer_consensus_pick(choice: dict[str, Any]) -> dict[str, Any]:
    """Convert a rendered soccer choice into the shared comparison shape."""
    line = choice.get("line")
    return {
        "selection": _market_label(choice["market"], choice["game"]),
        "market": choice["market"],
        "match_id": choice["game"].get("match_id"),
        "line": line,
        "implied_probability": _implied_probability(line) if isinstance(line, (int, float)) else None,
        "estimated_probability": choice.get("estimated_probability"),
    }
