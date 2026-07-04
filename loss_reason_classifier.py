"""Loss reason classifier for BETGPTAI AI Learning Engine Phase 6."""

from __future__ import annotations

from typing import Any


LOSS_REASON_TAGS = {
    "bullpen_failure",
    "starting_pitcher_underperformed",
    "starting_pitcher_scratched",
    "lineup_scratch",
    "lineup_not_confirmed",
    "weather_shift",
    "wind_changed",
    "park_factor_misread",
    "travel_spot_misread",
    "bad_statcast_matchup",
    "bad_pitch_type_matchup",
    "offense_cold",
    "bullpen_fatigue",
    "late_scoring_variance",
    "extra_innings_variance",
    "market_bad_price",
    "wrong_total_environment",
    "player_not_in_lineup",
    "player_team_mismatch",
    "player_cold_streak",
    "bad_bvp_read",
    "unknown_variance",
}


TAG_TO_WEIGHT = {
    "bullpen_failure": ("bullpen_edge", -0.03),
    "bullpen_fatigue": ("bullpen_fatigue", 0.04),
    "starting_pitcher_underperformed": ("starting_pitcher_edge", -0.02),
    "starting_pitcher_scratched": ("starting_pitcher_edge", -0.04),
    "lineup_scratch": ("lineup_confirmation", 0.04),
    "lineup_not_confirmed": ("lineup_confirmation", 0.04),
    "weather_shift": ("weather_edge", 0.03),
    "wind_changed": ("weather_edge", 0.04),
    "park_factor_misread": ("park_factor", 0.03),
    "travel_spot_misread": ("travel_rest", 0.03),
    "bad_statcast_matchup": ("statcast_contact", 0.03),
    "bad_pitch_type_matchup": ("pitch_type_matchup", 0.03),
    "offense_cold": ("recent_form", 0.03),
    "late_scoring_variance": ("market_value", -0.01),
    "extra_innings_variance": ("market_value", -0.01),
    "market_bad_price": ("market_value", 0.04),
    "wrong_total_environment": ("weather_edge", 0.03),
    "player_not_in_lineup": ("player_lineup_spot", 0.05),
    "player_team_mismatch": ("player_team_verification", 0.05),
    "player_cold_streak": ("player_streaks", 0.03),
    "bad_bvp_read": ("bvp", -0.03),
}


def _text(pick: dict[str, Any]) -> str:
    return " ".join(
        str(pick.get(key) or "")
        for key in ("pick_text", "selection", "market_type", "pick_type", "category")
    ).lower()


def _market(pick: dict[str, Any]) -> str:
    return str(pick.get("market_type") or pick.get("pick_type") or "").lower()


def classify_loss(
    pick: dict[str, Any],
    *,
    game_context: dict[str, Any] | None = None,
    model_report: dict[str, Any] | None = None,
    props_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify a losing pick with best-effort tags and notes.

    Missing supporting data is not treated as fatal; the classifier falls back
    to unknown_variance as requested.
    """
    game_context = game_context or {}
    model_report = model_report or {}
    props_context = props_context or {}
    tags: list[str] = []
    notes: list[str] = []
    text = _text(pick)
    market = _market(pick)

    if market in {"total", "team_total"} or "over" in text or "under" in text:
        if not isinstance(game_context.get("weather"), dict):
            tags.append("wrong_total_environment")
            notes.append("Weather/run environment context was unavailable or incomplete.")
        else:
            tags.append("late_scoring_variance")
            notes.append("Total/team-total loss may include run-timing variance.")

    if market in {"moneyline", "f5_moneyline", "runline"}:
        if not game_context.get("away_pitcher") or not game_context.get("home_pitcher"):
            tags.append("starting_pitcher_scratched")
            notes.append("Probable pitcher metadata was missing or unstable.")
        else:
            tags.append("starting_pitcher_underperformed")
            notes.append("Side/spread loss likely tied to starter or matchup underperformance.")

    if market in {"team_total", "runline", "moneyline"}:
        if not isinstance(game_context.get("savant"), dict):
            tags.append("bad_statcast_matchup")
            notes.append("Statcast enrichment was unavailable for this matchup.")

    if "prop" in text or market in {"hits", "home_runs", "strikeouts", "total_bases"}:
        if not props_context:
            tags.append("player_not_in_lineup")
            notes.append("Prop context was unavailable when reviewing the loss.")
        else:
            verification = props_context.get("player_verification")
            lineup = props_context.get("lineup_verification")
            if isinstance(verification, dict) and not verification.get("verified"):
                tags.append("player_team_mismatch")
                notes.append("Player/team verification was not clean.")
            if isinstance(lineup, dict) and not lineup.get("verified"):
                tags.append("player_not_in_lineup")
                notes.append("Lineup verification was not clean.")
            streak = props_context.get("hitting_streak")
            if isinstance(streak, dict) and streak.get("available"):
                try:
                    if int(streak.get("games_with_hit_streak") or 0) == 0:
                        tags.append("player_cold_streak")
                        notes.append("Player did not have active hit-streak support.")
                except (TypeError, ValueError):
                    pass

    sources = model_report.get("sources") if isinstance(model_report.get("sources"), dict) else {}
    if sources and not sources.get("baseball_savant"):
        tags.append("bad_statcast_matchup")
    if sources and not sources.get("weather"):
        tags.append("weather_shift")

    if "extra" in str(pick.get("last_grading_error") or "").lower():
        tags.append("extra_innings_variance")
    odds = pick.get("odds")
    try:
        if odds is not None and abs(float(odds)) > 180:
            tags.append("market_bad_price")
            notes.append("Price was expensive; market value should be reviewed.")
    except (TypeError, ValueError):
        pass

    clean_tags = []
    for tag in tags:
        if tag in LOSS_REASON_TAGS and tag not in clean_tags:
            clean_tags.append(tag)
    if not clean_tags:
        clean_tags = ["unknown_variance"]
        notes.append("No reliable supporting data identified a specific cause.")
    return {
        "loss_reason_tags": clean_tags,
        "notes": notes or ["Best-effort review completed."],
    }


def suggested_weight_changes_from_tags(tags: list[str]) -> dict[str, dict[str, Any]]:
    """Convert loss tags into small, capped daily weight suggestions."""
    suggestions: dict[str, dict[str, Any]] = {}
    for tag in tags:
        mapping = TAG_TO_WEIGHT.get(tag)
        if not mapping:
            continue
        factor, change = mapping
        current = suggestions.setdefault(
            factor,
            {
                "suggested_change": 0.0,
                "reason_tags": [],
                "reason": "",
            },
        )
        current["suggested_change"] += change
        current["reason_tags"].append(tag)
    for factor, item in suggestions.items():
        change = max(-0.05, min(0.05, float(item["suggested_change"])))
        item["suggested_change"] = round(change, 4)
        tags_text = ", ".join(item["reason_tags"])
        item["reason"] = f"Suggested from loss tags: {tags_text}"
    return suggestions
