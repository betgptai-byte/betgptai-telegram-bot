"""Anime Vault image workflow for BETGPTAI /today Play of the Day.

This module is preview-only. It saves a prompt every time and generates an image
only when IMAGE_GENERATION_ENABLED is true. It never posts to public channels.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from game_time import format_game_clock
from openai_image_generator import generate_image_from_prompt


BASE_DIR = Path(__file__).resolve().parent

ANIME_VAULT_STYLE = (
    "BETGPTAI Anime Vault, 1080x1920 vertical premium anime sports magazine card, "
    "dark electric baseball stadium background, team mascot artwork, manga action "
    "panels, glowing borders, team-color lightning, dramatic stadium lights, "
    "dynamic action pose, bold brush typography, premium ESPN x Topps x anime "
    "sports-card style"
)
NEGATIVE_STYLE = (
    "no emojis, no smiley faces, no placeholder icons, no flat infographic style"
)


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any, fallback: str = "TBD") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _market_from_pick(pick: str) -> str:
    text = pick.upper()
    if " F5 " in f" {text} ":
        return "F5 Moneyline"
    if "OVER" in text or "UNDER" in text:
        return "Total"
    if "-1.5" in text or "+1.5" in text:
        return "Runline"
    if " ML" in text or "MONEYLINE" in text:
        return "Moneyline"
    return "Play of the Day"


def _short_reason(featured: dict[str, Any]) -> str:
    reason = _clean(featured.get("reason"), "")
    if reason and reason != "TBD":
        return reason[:180]
    return "Model-supported matchup edge with a singles-first profile."


def create_today_pick_prompt(featured: dict[str, Any], card_date: str) -> str:
    """Create the 1080x1920 Anime Vault prompt for today's featured pick."""
    pick = _clean(featured.get("play_of_day"), "Play of the Day")
    game = featured.get("play_game") if isinstance(featured.get("play_game"), dict) else {}
    away = _clean(game.get("away_team"), "Away Team")
    home = _clean(game.get("home_team"), "Home Team")
    game_time = format_game_clock(game.get("game_time"), status=game.get("status")).replace("🕒 ", "")
    risk = featured.get("risk_grade")
    confidence = f"{risk}/10" if isinstance(risk, (int, float)) and not isinstance(risk, bool) else "7/10"
    market = _market_from_pick(pick)
    reason = _short_reason(featured)

    return (
        f"{ANIME_VAULT_STYLE}. Create one official admin-preview image card for "
        f"BETGPTAI Today's Pick / Play of the Day. Format: 1080x1920 vertical. "
        f"TOP title text: TODAY'S PICK. Main card text must stay simple and must "
        f"not show odds: Pick: {pick}; Matchup: {away} @ {home}; Game Time ET: "
        f"{game_time}; Market: {market}; Confidence Grade: {confidence}; Reason: "
        f"{reason}. Include a singles-first disclaimer: Singles are recommended. "
        f"Parlays carry greater risk. Educational analysis only. Use team mascot "
        f"or anime baseball hero artwork in a large center action pose with "
        f"electric lightning and glowing sports-card panels. Do not show sportsbook "
        f"names, API names, AI disagreement, raw model scores, or internal rules. "
        f"{NEGATIVE_STYLE}."
    )


def prepare_today_pick_image(
    featured: dict[str, Any],
    card_date: str,
    *,
    output_root: str | Path | None = None,
    image_generation_enabled: bool | None = None,
) -> dict[str, Any]:
    """Save today's pick prompt and optionally generate the preview image."""
    output_base = Path(output_root) if output_root else BASE_DIR / "generated_cards"
    output_dir = output_base / card_date
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = create_today_pick_prompt(featured, card_date)
    prompt_path = output_dir / "today_pick_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    image_path = output_dir / "today_pick.png"
    image_error = None

    enabled = _truthy_env("IMAGE_GENERATION_ENABLED") if image_generation_enabled is None else image_generation_enabled
    if enabled:
        try:
            generate_image_from_prompt(prompt, str(image_path))
        except Exception as error:
            image_error = str(error)

    return {
        "status": "ready",
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "image_path": str(image_path) if image_path.exists() else None,
        "image_error": image_error,
    }
