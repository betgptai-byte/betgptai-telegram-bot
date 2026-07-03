"""BETGPTAI Anime Edition v7.0 MLB carousel prompt generator.

This module does not create placeholder images. It creates elite, production-
ready prompts for ChatGPT image generation and saves those prompts as text files.
Final artwork is generated outside this Python workflow.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from mascot_style import MLB_MASCOT_STYLE, MLB_TEAM_ALIASES


IMAGE_SIZE = "1080x1920"
NEGATIVE_STYLE = (
    "no emojis, no smiley faces, no placeholder icons, no flat infographic style"
)
BASE_VISUAL_STYLE = (
    "BETGPTAI Anime Edition v7.0, 1080x1920 vertical 9:16 carousel slide, "
    "match the BETGPTAI Anime Vault reference quality: dark cinematic stadium, "
    "explosive electric lightning background, neon yellow blue red and green "
    "glowing panels, manga action panels, bold brush typography, high-energy "
    "anime sports trading card, premium collectible sports card illustration, "
    "Topps Chrome sports card aesthetic, ESPN-style sports hierarchy, Blue Lock "
    "sports anime energy, cel-shaded anime rendering, dramatic perspective, "
    "dynamic manga speed lines, stadium atmosphere, team-color energy effects, "
    "clean readable typography for mobile, social-media ready for Instagram "
    "Facebook TikTok"
)
HERO_DIRECTION = (
    "Every featured mascot or player must have an aggressive action pose, "
    "glowing eyes, hyper-detailed uniform, high-detail mascot face, electric "
    "lightning effects, championship-level intensity, dramatic perspective, and "
    "must occupy 35-45% of the composition as the hero of the card. Avoid a "
    "generic anime character, flat poster, stock illustration, or mascot "
    "standing still"
)
QUALITY_LOCK = (
    "Mandatory quality lock: aggressive action pose, glowing eyes, dynamic manga "
    "speed lines, dramatic perspective, premium sports-card illustration, "
    "electric lightning effects, stadium atmosphere, cel-shaded anime rendering, "
    "hyper-detailed uniform, team-color energy effects, high-detail mascot face, "
    "championship-level intensity, Topps Chrome sports card aesthetic, Blue Lock "
    "sports anime energy, premium collectible card quality"
)


def _as_list(value: Any, limit: int = 5) -> list[str]:
    """Return a short list of clean text items from flexible card data."""
    if isinstance(value, str):
        raw_items = re.split(r"\n+|,\s*(?=[A-Z0-9])", value)
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    cleaned = [re.sub(r"\s+", " ", item).strip(" -•") for item in raw_items]
    return [item for item in cleaned if item][:limit]


def _pick(card_data: dict[str, Any], key: str, default: Any) -> Any:
    """Read a value from card_data while keeping prompt generation forgiving."""
    value = card_data.get(key)
    return default if value in (None, "", []) else value


def _find_team_name(text: str) -> str | None:
    """Find the first MLB team referenced in a pick or matchup string."""
    lowered = text.lower()
    for team in MLB_MASCOT_STYLE:
        if team.lower() in lowered:
            return team
    for alias, team in MLB_TEAM_ALIASES.items():
        if re.search(rf"\b{re.escape(alias.lower())}\b", lowered):
            return team
    return None


def _mascot_style_for(text: str, fallback: str = "anime baseball hero mascot") -> str:
    """Return the team-specific anime mascot prompt style for a pick."""
    team = _find_team_name(text)
    if team and team in MLB_MASCOT_STYLE:
        return MLB_MASCOT_STYLE[team]
    return (
        f"{fallback}, aggressive action pose, glowing eyes, dynamic manga speed "
        f"lines, dramatic perspective, premium sports-card illustration, electric "
        f"lightning effects, stadium atmosphere, cel-shaded anime rendering, "
        f"hyper-detailed uniform, team-color energy effects, high-detail mascot "
        f"face, championship-level intensity, Topps Chrome sports card aesthetic, "
        f"Blue Lock sports anime energy, premium collectible card quality, mascot "
        f"occupies 35-45% of the composition and feels like the hero of the card"
    )


def _compact_block(title: str, values: list[str]) -> str:
    """Create concise layout text guidance for one slide section."""
    if not values:
        values = ["TBD"]
    numbered = "; ".join(f"{index}. {value}" for index, value in enumerate(values, 1))
    return f"{title}: {numbered}"


def create_slide_prompt(slide_number: int, card_data: dict[str, Any]) -> str:
    """Create one BETGPTAI Anime Edition v7.0 MLB image prompt.

    The prompt is meant for ChatGPT image generation. It asks for a real anime
    sports-card layout and explicitly rejects placeholder graphics.
    """
    play_of_day = str(_pick(card_data, "play_of_day", "Official Play of the Day TBD"))
    best_bet = str(_pick(card_data, "best_bet", play_of_day))
    safe_parlay = _as_list(_pick(card_data, "safe_parlay", []), 3)
    value_parlay = _as_list(_pick(card_data, "value_parlay", []), 4)
    ev_parlay = _as_list(_pick(card_data, "ev_parlay", value_parlay), 4)
    core_five = _as_list(_pick(card_data, "core_five", []), 5)
    moneylines = _as_list(_pick(card_data, "moneylines", []), 5)
    f5 = _as_list(_pick(card_data, "f5", []), 5)
    runlines = _as_list(_pick(card_data, "runlines", []), 5)
    totals = _as_list(_pick(card_data, "totals", []), 5)
    team_totals = _as_list(_pick(card_data, "team_totals", []), 5)

    common_rules = (
        "Use short clean betting text only. Do not use confidence meters. Use "
        "illustrated gold stars only for rating accents. Do not include internal "
        "betting system names, formulas, model rules, API names, or long explanations. "
        "Keep all panels readable on a phone. The image must look like a premium "
        "collectible sports card, not a generic poster. "
        f"{QUALITY_LOCK}"
    )

    if slide_number == 1:
        mascot = _mascot_style_for(play_of_day, "anime ace starting pitcher or team mascot")
        return (
            f"{BASE_VISUAL_STYLE}. Slide 1 of 7: PLAY OF THE DAY. Gold legendary "
            f"theme with a giant center hero card. Feature artwork: {mascot}. "
            f"Main text: PLAY OF THE DAY, {play_of_day}. Add a dramatic gold vault "
            f"frame, stadium spotlights, lightning, manga action pose, premium card "
            f"shine, and space for one short reason. {HERO_DIRECTION}. "
            f"{common_rules}. {NEGATIVE_STYLE}."
        )

    if slide_number == 2:
        mascot = _mascot_style_for(" ".join(moneylines), "anime MLB mascot card lineup")
        return (
            f"{BASE_VISUAL_STYLE}. Slide 2 of 7: TOP 5 MONEYLINES. Red neon theme, "
            f"five stacked mascot trading-card panels, team-color highlights. "
            f"Featured mascot direction: {mascot}. {_compact_block('Moneylines', moneylines)}. "
            f"{HERO_DIRECTION}. {common_rules}. {NEGATIVE_STYLE}."
        )

    if slide_number == 3:
        mascot = _mascot_style_for(" ".join(f5), "anime starting pitcher hero")
        return (
            f"{BASE_VISUAL_STYLE}. Slide 3 of 7: TOP 5 F5 PLAYS. Electric blue "
            f"theme, first-five-innings pitcher battle energy, fastball motion "
            f"trails, cold stadium light, manga speed panels. Featured artwork: "
            f"{mascot}. {_compact_block('F5 Moneyline Plays', f5)}. "
            f"{HERO_DIRECTION}. {common_rules}. {NEGATIVE_STYLE}."
        )

    if slide_number == 4:
        mascot = _mascot_style_for(" ".join(runlines), "anime power-hitting team mascot")
        return (
            f"{BASE_VISUAL_STYLE}. Slide 4 of 7: TOP 5 RUN LINES. Crimson impact "
            f"theme, explosive batting action, cracked-glass manga panels, heavy "
            f"stadium shadows, intense team-color glows. Featured artwork: {mascot}. "
            f"{_compact_block('Run Lines', runlines)}. {HERO_DIRECTION}. "
            f"{common_rules}. {NEGATIVE_STYLE}."
        )

    if slide_number == 5:
        combined = " ".join(totals + team_totals)
        mascot = _mascot_style_for(combined, "anime slugger and pitcher duel")
        return (
            f"{BASE_VISUAL_STYLE}. Slide 5 of 7: GAME TOTALS AND TEAM TOTALS. "
            f"Orange and green theme, weather/park run-environment energy, glowing "
            f"scoreboard panels, baseballs streaking through lightning. Featured "
            f"artwork: {mascot}. {_compact_block('Game Totals', totals)}. "
            f"{_compact_block('Team Totals', team_totals)}. {common_rules}. "
            f"{HERO_DIRECTION}. {NEGATIVE_STYLE}."
        )

    if slide_number == 6:
        mascot = _mascot_style_for(
            best_bet,
            "anime vault guardian baseball mascot collage",
        )
        return (
            f"{BASE_VISUAL_STYLE}. Slide 6 of 7: THE VAULT. Premium vault-board "
            f"layout with glowing gold, green, purple, and blue panels. Feature "
            f"artwork: {mascot}. Sections: Best Bet: {best_bet}; "
            f"{_compact_block('Safe Parlay', safe_parlay)}; "
            f"{_compact_block('Value Parlay', value_parlay)}; "
            f"{_compact_block('+EV Parlay', ev_parlay)}; "
            f"{_compact_block('Core Five', core_five)}. {HERO_DIRECTION}. "
            f"{common_rules}. {NEGATIVE_STYLE}."
        )

    if slide_number == 7:
        return (
            f"{BASE_VISUAL_STYLE}. Slide 7 of 7: FOLLOW @BETGPTAI. Feature the "
            f"anime Odds Reaper mascot, original dark-haired sports-anime character "
            f"in a BETGPTAI hoodie pointing toward the viewer, blue lightning aura, "
            f"stadium smoke, gold vault border, cinematic finale poster. Text: "
            f"FOLLOW @BETGPTAI, THE ODDS REAPER, SINGLES FIRST, PARLAYS OPTIONAL, "
            f"EDUCATIONAL ANALYSIS ONLY. Keep typography bold and readable. "
            f"The Odds Reaper must occupy 35-45% of the composition with an "
            f"aggressive action pose, glowing eyes, dynamic manga speed lines, "
            f"dramatic perspective, cel-shaded anime rendering, and championship-level "
            f"intensity. {common_rules}. {NEGATIVE_STYLE}."
        )

    raise ValueError("slide_number must be between 1 and 7.")


def generate_mlb_card_slides(card_data: dict[str, Any]) -> list[str]:
    """Generate the seven BETGPTAI Anime Edition v7.0 carousel prompts."""
    return [create_slide_prompt(slide_number, card_data) for slide_number in range(1, 8)]


def save_mlb_card_slide_prompts(
    prompts: list[str], output_dir: str | Path = "generated_cards"
) -> list[Path]:
    """Save slide prompts as generated_cards/slide_N_prompt.txt files."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    saved_paths: list[Path] = []
    for index, prompt in enumerate(prompts, start=1):
        path = destination / f"slide_{index}_prompt.txt"
        path.write_text(prompt, encoding="utf-8")
        saved_paths.append(path)
    return saved_paths
