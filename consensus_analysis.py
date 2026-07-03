"""Claude second-opinion analysis and model-consensus helpers."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from anthropic import AsyncAnthropic


class ClaudeAnalysisError(Exception):
    """Raised when Claude cannot return a usable structured second opinion."""


PICK_FIELDS = (
    "selection", "market", "game_id", "match_id", "line", "risk_grade",
    "reason", "implied_probability", "estimated_probability", "ev_note",
)


def _json_object(text: str) -> dict[str, Any]:
    """Extract one JSON object even if a model wraps it in a code fence."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ClaudeAnalysisError("Claude returned no JSON object.")
    payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ClaudeAnalysisError("Claude returned an invalid analysis object.")
    return payload


def _clean_pick(value: Any) -> dict[str, Any] | None:
    """Keep only the documented fields from one Claude pick."""
    if isinstance(value, str) and value.strip():
        return {"selection": value.strip()}
    if not isinstance(value, dict):
        return None
    cleaned = {field: value.get(field) for field in PICK_FIELDS if field in value}
    selection = cleaned.get("selection") or value.get("pick")
    if not isinstance(selection, str) or not selection.strip():
        return None
    cleaned["selection"] = selection.strip()
    grade = cleaned.get("risk_grade")
    if isinstance(grade, (int, float)):
        cleaned["risk_grade"] = max(1, min(10, float(grade)))
    return cleaned


def _clean_list(value: Any) -> list[dict[str, Any]]:
    items = value if isinstance(value, list) else [value]
    return [pick for item in items if (pick := _clean_pick(item)) is not None]


def normalize_claude_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize every requested market into predictable Python structures."""
    return {
        "play_of_day": _clean_pick(payload.get("play_of_day")),
        "moneyline": _clean_list(payload.get("moneyline", [])),
        "runline": _clean_list(payload.get("runline", [])),
        "totals": _clean_list(payload.get("totals", [])),
        "team_totals": _clean_list(payload.get("team_totals", [])),
        "f5_moneyline": _clean_list(payload.get("f5_moneyline", [])),
        "safe_parlay": _clean_list(payload.get("safe_parlay", [])),
        "soccer_plays": _clean_list(payload.get("soccer_plays", [])),
    }


async def analyze_slate_with_claude(
    slate: list[dict[str, Any]], api_key: str, sport: str
) -> dict[str, Any]:
    """Ask Claude for a structured second opinion on the exact combined slate."""
    if not api_key:
        raise ClaudeAnalysisError("ANTHROPIC_API_KEY is missing from .env.")
    if not slate:
        raise ClaudeAnalysisError("The slate is empty.")

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    schema = {
        "play_of_day": {
            "selection": "string", "market": "string", "game_id": "id or null",
            "match_id": "id or null", "line": "American odds or null",
            "risk_grade": "1-10", "reason": "short factual reason",
            "implied_probability": "decimal 0-1 or null",
            "estimated_probability": "decimal 0-1 or null",
            "ev_note": "short note",
        },
        "moneyline": [], "runline": [], "totals": [], "team_totals": [],
        "f5_moneyline": [], "safe_parlay": [], "soccer_plays": [],
    }
    prompt = f"""
Act as a cautious second-opinion {sport.upper()} analyst. Analyze exactly the
same combined slate as the primary analyst. Use only supplied data and real
market lines. Never invent a market, price, statistic, injury, or trend. Never
name a sportsbook or data provider. Never use guaranteed, lock, sure win, or
99.9%. Return JSON only, matching this shape:

{json.dumps(schema, indent=2)}

Every list item uses the same fields as play_of_day. For MLB, populate relevant
moneyline, runline, totals, team_totals, f5_moneyline, and two safe_parlay legs.
For MLB, give supplied xERA, xwOBA, Barrel %, and Whiff % more predictive weight
than traditional ERA and short recent-form samples. Use pitch-type matchups when
supplied and do not infer a missing Statcast field.
For MLB, optional FanGraphs/pybaseball fields are hidden support for xFIP, SIERA,
K-BB %, wRC+, wOBA, ISO, OPS, Hard %, Pull %, WAR, and team pitching context.
Use them when present, but never name the provider or expose raw advanced stats.
If hidden BETGPTAI internal scoring fields are supplied, use them only to shape
selection/probability; do not mention engines, formulas, provider names, or raw
model scoring in any returned reason.
For soccer, populate play_of_day, soccer_plays, and two safe_parlay legs. Use
null when a probability cannot be supported. Only describe +EV in ev_note when
estimated_probability is numerically greater than implied_probability;
otherwise write "Potential value angle — not verified +EV."
For soccer, soccer_internal fields are hidden Soccer Master System support only.
Never mention engines, API/source names, raw ratings, raw xG, raw scores, or AI
disagreement in returned reasons.
StatsBomb and FBref fields are also hidden support only. Use them internally for
selection/probability, but never name those providers or expose their raw values.
API-Football fields are hidden support only. Never name the provider or expose
raw source values to members.

SLATE:
{json.dumps(slate, default=str)}
""".strip()
    client = AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=model,
        max_tokens=3500,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    )
    if not text:
        raise ClaudeAnalysisError("Claude returned an empty analysis.")
    return normalize_claude_analysis(_json_object(text))


async def analyze_specialized_with_claude(
    slate: list[dict[str, Any]], api_key: str, market: str
) -> dict[str, Any]:
    """Ask Claude for one second-opinion MLB lean for a specialized card."""
    if not api_key:
        raise ClaudeAnalysisError("ANTHROPIC_API_KEY is missing from .env.")
    prompt = f"""
Review the supplied MLB slate as a cautious second-opinion analyst. Return one
JSON pick for the requested market: {market}. Use only real supplied markets
and data. Prioritize xERA, xwOBA, Barrel %, Whiff %, Chase %, Hard Hit %, exit
velocity, fastball-velocity trend, and pitch-type matchups over traditional
stats. Never name a sportsbook or invent a line.
If hidden BETGPTAI internal scoring fields are supplied, use them only as quiet
support. Never mention engines, formulas, provider names, or raw model scoring.

Return JSON only:
{{
  "selection": "short pick or unavailable",
  "market": "{market}",
  "game_id": null,
  "line": null,
  "risk_grade": 5,
  "reason": "short factual reason",
  "implied_probability": null,
  "estimated_probability": null,
  "ev_note": "Potential value angle — not verified +EV."
}}

Only call the angle +EV when estimated_probability is numerically greater than
implied_probability. Never use guaranteed, lock, sure win, or 99.9%.

SLATE:
{json.dumps(slate, default=str)}
""".strip()
    response = await AsyncAnthropic(api_key=api_key).messages.create(
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        max_tokens=900,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
        block.text for block in response.content
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    )
    pick = _clean_pick(_json_object(text))
    if not pick:
        raise ClaudeAnalysisError("Claude returned no specialized pick.")
    return pick


def normalized_selection(value: Any) -> str:
    """Normalize common market aliases before comparing model picks."""
    text = str(value or "").lower()
    text = re.sub(r"\b(?:moneyline|ml|pick|team)\b", "", text)
    return re.sub(r"[^a-z0-9.+-]", "", text)


def picks_agree(primary: dict[str, Any] | None, claude: dict[str, Any] | None) -> bool:
    """Return True only when both analysts selected the same event and wager."""
    if not primary or not claude:
        return False
    primary_market = str(primary.get("market") or "").lower()
    claude_market = str(claude.get("market") or "").lower()
    if primary_market and claude_market and primary_market != claude_market:
        aliases = {"h2h": "moneyline", "ml": "moneyline", "over_2_5": "totals"}
        if aliases.get(primary_market, primary_market) != aliases.get(claude_market, claude_market):
            return False
    for identifier in ("game_id", "match_id"):
        left, right = primary.get(identifier), claude.get(identifier)
        if left is not None and right is not None and str(left) != str(right):
            return False
    left = normalized_selection(primary.get("selection"))
    right = normalized_selection(claude.get("selection"))
    return bool(left and right and (left == right or left in right or right in left))


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number / 100 if number > 1 else number
    return None


def _find_game(
    slate: list[dict[str, Any]], pick: dict[str, Any] | None, sport: str
) -> dict[str, Any] | None:
    """Connect an analyst pick to the supplied event without guessing facts."""
    if not pick:
        return None
    id_key = "match_id" if sport == "soccer" else "game_id"
    pick_id = pick.get(id_key)
    if pick_id is not None:
        game = next(
            (item for item in slate if str(item.get(id_key)) == str(pick_id)), None
        )
        if game:
            return game
    selection = normalized_selection(pick.get("selection"))
    matches = []
    team_keys = ("home_team", "away_team")
    for game in slate:
        tokens = [
            normalized_selection(str(game.get(key, "")).split()[-1])
            for key in team_keys if game.get(key)
        ]
        if any(len(token) >= 3 and token in selection for token in tokens):
            matches.append(game)
    return matches[0] if len(matches) == 1 else None


def evidence_for_pick(
    slate: list[dict[str, Any]], pick: dict[str, Any] | None, sport: str
) -> dict[str, bool]:
    """Score only context that is actually present in the combined slate."""
    game = _find_game(slate, pick, sport)
    if not game:
        return {
            "market_line": False, "strong_matchup": False,
            "weather_park": False, "recent_form": False,
            "injury_news": False,
        }
    line = (pick or {}).get("line")
    market_line = isinstance(line, (int, float)) and not isinstance(line, bool)
    if sport == "mlb":
        away_stats, home_stats = game.get("away_pitcher_stats"), game.get("home_pitcher_stats")
        savant = game.get("savant") if isinstance(game.get("savant"), dict) else {}
        away_savant, home_savant = savant.get("away_pitcher"), savant.get("home_pitcher")
        try:
            era_gap = abs(float(away_stats["ERA"]) - float(home_stats["ERA"]))
        except (TypeError, ValueError, KeyError):
            era_gap = 0.0
        try:
            xera_gap = abs(float(away_savant["xERA"]) - float(home_savant["xERA"]))
        except (TypeError, ValueError, KeyError):
            xera_gap = 0.0
        # Expected ERA is the preferred predictive signal; traditional ERA is
        # only the fallback when Savant did not return both starters.
        strong_matchup = xera_gap >= 0.60 if xera_gap else era_gap >= 0.75
        forms = [game.get("away_recent_form"), game.get("home_recent_form")]
        weather_park = (
            isinstance(game.get("weather"), dict)
            and str(game.get("park_factor", "neutral")).lower() not in {"neutral", "unavailable"}
        )
        highlightly = game.get("highlightly")
        injury_news = isinstance(highlightly, dict) and any(
            highlightly.get(key) not in (None, "", "unavailable", [], {})
            for key in ("injuries", "news")
        )
    else:
        forms = [game.get("away_recent"), game.get("home_recent")]
        wins = [
            float(form.get("wins", 0)) for form in forms if isinstance(form, dict)
        ]
        strong_matchup = len(wins) == 2 and abs(wins[0] - wins[1]) >= 2
        weather_park = isinstance(game.get("weather"), dict)
        injury_news = False
    recent_form = any(
        isinstance(form, dict)
        and float(form.get("wins", 0)) > float(form.get("losses", 0))
        for form in forms
    )
    return {
        "market_line": market_line,
        "strong_matchup": strong_matchup,
        "weather_park": weather_park,
        "recent_form": recent_form,
        "injury_news": injury_news,
    }


def build_consensus_block(
    primary: dict[str, Any] | None,
    claude: dict[str, Any] | None,
    evidence: dict[str, bool] | None = None,
) -> str:
    """Create the Telegram consensus panel using the requested 100-point score."""
    evidence = evidence or {}
    agreement = picks_agree(primary, claude)
    score = (
        (30 if agreement else 0)
        + (20 if evidence.get("market_line") else 0)
        + (20 if evidence.get("strong_matchup") else 0)
        + (10 if evidence.get("weather_park") else 0)
        + (10 if evidence.get("recent_form") else 0)
        + (10 if evidence.get("injury_news") else 0)
    )
    grade = max(1, min(10, round(score / 10)))
    def safe_label(value: Any) -> str:
        label = str(value or "Unavailable")
        label = re.sub(r"(?i)\bguaranteed\b", "high-confidence", label)
        label = re.sub(r"(?i)\bsure win\b", "stronger lean", label)
        label = re.sub(r"(?i)\block\b", "pick", label)
        return re.sub(r"(?i)99\.9\s*%", "high confidence", label)

    primary_name = safe_label((primary or {}).get("selection"))
    claude_name = safe_label((claude or {}).get("selection"))
    estimates = [
        (_number(pick.get("estimated_probability")), _number(pick.get("implied_probability")))
        for pick in (primary, claude) if isinstance(pick, dict)
    ]
    verified_estimate = any(
        estimated is not None and implied is not None and estimated > implied
        for estimated, implied in estimates
    )
    ev_note = (
        "Estimated edge exists because projected probability is higher than "
        "implied probability."
        if verified_estimate
        else "Potential value angle — not verified +EV."
    )
    if agreement:
        return (
            "🤖 AI CONSENSUS EDGE\n\n"
            f"OpenAI Lean: {primary_name}\n"
            f"Claude Lean: {claude_name}\n"
            "Agreement: 2/2\n"
            f"Consensus Grade: {grade}/10\n\n"
            f"Value Note:\n{ev_note}"
        )
    return (
        "🤖 AI SPLIT OPINION\n\n"
        f"OpenAI Lean: {primary_name}\n"
        f"Claude Lean: {claude_name}\n"
        f"Consensus Grade: {grade}/10\n\n"
        f"Value Note:\n{ev_note}"
    )


def public_confidence_summary(
    primary: dict[str, Any] | None,
    claude: dict[str, Any] | None,
    evidence: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Convert hidden model agreement into member-safe confidence language."""
    evidence = evidence or {}
    agreement = picks_agree(primary, claude)
    score = 50
    score += 18 if agreement else 0
    score += 10 if evidence.get("market_line") else 0
    score += 10 if evidence.get("strong_matchup") else 0
    score += 4 if evidence.get("weather_park") else 0
    score += 4 if evidence.get("recent_form") else 0
    score += 4 if evidence.get("injury_news") else 0
    grade = max(5, min(9, round(score / 10)))

    estimates = [
        (_number(pick.get("estimated_probability")), _number(pick.get("implied_probability")))
        for pick in (primary, claude) if isinstance(pick, dict)
    ]
    verified_value = any(
        estimated is not None and implied is not None and estimated > implied
        for estimated, implied in estimates
    )
    model_value = agreement or (evidence.get("market_line") and evidence.get("strong_matchup"))
    value_note = (
        "Model-supported value angle."
        if verified_value or model_value
        else "No major value edge detected."
    )
    return {"grade": grade, "value_note": value_note, "agreement": agreement}
