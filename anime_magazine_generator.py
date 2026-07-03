"""BETGPTAI Daily Anime Sports Magazine prompt generator.

This module creates prompt-ready Anime Vault magazine pages. It intentionally
does not create placeholder graphics. If image generation is enabled elsewhere,
the bot can pass these prompts to the OpenAI Images API for owner previews.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from prop_card_generator import ANIME_VAULT_STYLE, NEGATIVE_STYLE, create_prop_card_prompt


def _clean(value: Any, fallback: str = "TBD") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _pick_line(prop: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(prop, dict):
        return fallback
    player = _clean(prop.get("player_name"), "Player")
    prop_type = str(prop.get("prop_type") or "").replace("_", " ").title()
    confidence = _clean(prop.get("confidence_grade"), "Confidence TBD")
    return f"{player} — {prop_type} — {confidence}"


def create_magazine_prompt(
    section_number: int,
    props_payload: dict[str, Any],
    mlb_card_data: dict[str, Any] | None = None,
    results_summary: str = "",
) -> str:
    """Create one prompt for a BETGPTAI Daily Anime Sports Magazine page."""
    mlb_card_data = mlb_card_data or {}
    display_date = _clean(props_payload.get("display_date"), "Today")
    best_hit = props_payload.get("best_hit")
    hr_watch = props_payload.get("hr_watch")
    best_strikeout = props_payload.get("best_strikeout")
    hits_by_team = props_payload.get("hits_by_team") or {}
    hit_lines = [
        _pick_line(prop, f"{team}: No qualified hit prop found")
        for team, prop in list(hits_by_team.items())[:12]
    ]
    mlb_plays = mlb_card_data.get("core_five") or mlb_card_data.get("moneylines") or []
    if isinstance(mlb_plays, str):
        mlb_plays = [mlb_plays]
    mlb_plays = [_clean(play) for play in mlb_plays[:5]]

    common = (
        f"{ANIME_VAULT_STYLE}. Daily Anime Sports Magazine page, date {display_date}. "
        "Use no public sportsbook names, no API names, no internal formulas, no raw "
        "model scores. Make it look like a premium anime betting magazine, not a "
        f"spreadsheet. {NEGATIVE_STYLE}."
    )

    if section_number == 1:
        return (
            f"{common} Section 1: COVER PAGE. Big title: BETGPTAI DAILY ANIME "
            f"SPORTS MAGAZINE. Subtitle: THE ODDS REAPER. Feature an original "
            f"anime baseball hero and vault guardian mascot in a dramatic stadium, "
            f"electric lightning, team-color neon, manga action panels, glossy "
            f"collector-magazine cover layout. Teaser lines: Best Hit Prop, HR "
            f"Watch, Strikeout Prop, Best MLB Plays, Victory Vault."
        )
    if section_number == 2 and isinstance(best_hit, dict):
        return create_prop_card_prompt(best_hit, "BEST HIT PROP")
    if section_number == 3 and isinstance(hr_watch, dict):
        return create_prop_card_prompt(hr_watch, "HR WATCH")
    if section_number == 4 and isinstance(best_strikeout, dict):
        return create_prop_card_prompt(best_strikeout, "BEST STRIKEOUT PROP")
    if section_number == 5:
        return (
            f"{common} Section 5: BEST HIT PROP PER TEAM. Create a glowing magazine "
            f"grid of verified hit prop candidates by team. Use team-color mini "
            f"panels and manga baseball energy. Visible text list: {'; '.join(hit_lines) or 'No qualified hit props found'}. "
            "Keep typography large and readable."
        )
    if section_number == 6:
        return (
            f"{common} Section 6: BEST MLB PLAYS. Create a premium Anime Vault "
            f"card board for official MLB plays. Use dark electric background, "
            f"gold borders, red and blue accent panels, baseball mascots in action. "
            f"Visible plays: {'; '.join(mlb_plays) or 'Official MLB plays loading'}. "
            "Keep text concise and mobile-readable."
        )
    if section_number == 7:
        return (
            f"{common} Section 7: RESULTS / VICTORY VAULT. Create a victory vault "
            f"results page with trophy lighting, anime scoreboard panels, electric "
            f"blue/gold celebration energy, and disciplined bankroll messaging. "
            f"Visible results summary: {_clean(results_summary, 'Results update pending')}. "
            "Footer text: Admin Preview Only. Educational analysis only."
        )
    return (
        f"{common} Section {section_number}: Admin preview filler page. Create a "
        "premium Anime Vault sports magazine page with clean readable typography."
    )


def generate_daily_magazine_prompts(
    props_payload: dict[str, Any],
    mlb_card_data: dict[str, Any] | None = None,
    results_summary: str = "",
) -> list[dict[str, str]]:
    """Create all seven magazine section prompts."""
    names = [
        "cover_page",
        "best_hit_prop",
        "hr_watch",
        "strikeout_prop",
        "hits_by_team",
        "best_mlb_plays",
        "results_victory_vault",
    ]
    return [
        {
            "name": names[index - 1],
            "title": names[index - 1].replace("_", " ").title(),
            "prompt": create_magazine_prompt(index, props_payload, mlb_card_data, results_summary),
        }
        for index in range(1, 8)
    ]


def save_magazine_prompts(
    prompt_items: list[dict[str, str]],
    output_dir: str | Path,
) -> list[Path]:
    """Save magazine prompts to generated_cards/YYYY-MM-DD/magazine."""
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, item in enumerate(prompt_items, start=1):
        path = directory / f"magazine_{index:02d}_{item['name']}_prompt.txt"
        path.write_text(item["prompt"], encoding="utf-8")
        saved.append(path)
    return saved
