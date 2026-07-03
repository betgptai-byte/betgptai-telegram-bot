"""Anime Vault prompt generator for admin-only MLB player prop cards."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


ANIME_VAULT_STYLE = (
    "BETGPTAI Anime Vault, 1080x1920 vertical sports magazine card, high-energy "
    "anime sports trading-card illustration, dark electric stadium background, "
    "team-color lightning, manga speed lines, glowing neon panels, dramatic "
    "perspective, cel-shaded rendering, hyper-detailed baseball uniform, premium "
    "Topps Chrome x ESPN x anime magazine aesthetic, bold readable typography, "
    "premium collectible card quality"
)
NEGATIVE_STYLE = (
    "no emojis, no smiley faces, no placeholder icons, no flat infographic style, "
    "no generic anime character, no stock illustration"
)


def _clean(value: Any, fallback: str = "Unavailable") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _prop_display(prop: dict[str, Any]) -> str:
    prop_type = str(prop.get("prop_type") or "").lower()
    line = prop.get("line")
    if prop_type == "home_runs":
        return "HR Watch"
    labels = {
        "hits": "Hits",
        "2_plus_hits": "Hits",
        "rbis": "RBIs",
        "runs": "Runs",
        "total_bases": "Total Bases",
        "walks": "Walks",
        "strikeouts": "Strikeouts",
        "pitcher_outs_recorded": "Pitcher Outs Recorded",
        "stolen_bases": "Stolen Bases",
    }
    if prop_type == "earned_runs":
        return f"Under {line} Earned Runs" if line is not None else "Earned Runs Lean"
    label = labels.get(prop_type, _clean(prop.get("market_type"), "Prop").replace("_", " ").title())
    return f"Over {line} {label}" if line is not None else label


def create_prop_card_prompt(prop: dict[str, Any], title: str) -> str:
    """Create one image prompt for a verified admin prop card."""
    verification = prop.get("player_verification") or {}
    verified_team = verification.get("current_team") or prop.get("team_name")
    player = _clean(prop.get("player_name"), "Player")
    team = _clean(verified_team, "Verified Team")
    opponent = _clean(prop.get("opponent_name"), "Opponent")
    game_time = _clean(prop.get("game_time_et"), "Time unavailable ET")
    prop_text = _prop_display(prop)
    confidence = _clean(prop.get("confidence_grade"), "Confidence TBD")
    reason = _clean(prop.get("reason"), "Verified matchup context supports the lean.")

    return (
        f"{ANIME_VAULT_STYLE}. Create an admin preview player prop card titled "
        f"{title}. Feature {player} as the hero athlete in an aggressive baseball "
        f"action pose, glowing eyes, dynamic manga speed lines, dramatic stadium "
        f"lighting, team-color energy effects for {team}, and a premium sports-card "
        f"layout. The player must occupy 35-45% of the composition. Visible card "
        f"text must be short and readable: Title: {title}; Player: {player}; "
        f"Verified Current Team: {team}; Opponent: {opponent}; Game Time ET: "
        f"{game_time}; Prop: {prop_text}; Confidence: {confidence}; Why: {reason}. "
        f"Use clean text labels instead of emoji icons. Include a small admin-only "
        f"preview label, but do not include API names, model names, raw stats, or "
        f"internal formulas. {NEGATIVE_STYLE}."
    )


def generate_prop_card_prompts(props_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return prompts for the main admin prop cards."""
    mapping = [
        ("best_hit", "BEST HIT PROP"),
        ("hr_watch", "HR WATCH"),
        ("best_strikeout", "BEST STRIKEOUT PROP"),
    ]
    prompts: list[dict[str, str]] = []
    for key, title in mapping:
        prop = props_payload.get(key)
        if isinstance(prop, dict):
            prompts.append({"name": key, "title": title, "prompt": create_prop_card_prompt(prop, title)})
    return prompts


def generate_hits_by_team_prompts(props_payload: dict[str, Any]) -> list[dict[str, str]]:
    """Return one Anime Vault prompt per team hit prop candidate."""
    prompts: list[dict[str, str]] = []
    hits_by_team = props_payload.get("hits_by_team") or {}
    for team in props_payload.get("teams_playing") or []:
        prop = hits_by_team.get(team)
        if isinstance(prop, dict):
            prompts.append({
                "name": f"hit_{re.sub(r'[^a-z0-9]+', '_', str(team).lower()).strip('_')}",
                "title": f"{team} BEST HIT PROP",
                "prompt": create_prop_card_prompt(prop, f"{team} BEST HIT PROP"),
            })
    return prompts


def save_prop_prompts(
    prompt_items: list[dict[str, str]],
    output_dir: str | Path,
) -> list[Path]:
    """Save prop prompts as text files for ChatGPT image generation."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, item in enumerate(prompt_items, start=1):
        safe_name = re.sub(r"[^a-z0-9]+", "_", item["name"].lower()).strip("_")
        path = directory / f"{index:02d}_{safe_name}_prompt.txt"
        path.write_text(item["prompt"], encoding="utf-8")
        saved.append(path)
    return saved
