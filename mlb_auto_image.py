"""Anime Vault image workflow for the free /mlb_auto card.

This is admin-preview only. It creates one 1080x1920 prompt/image for the free
MLB card and never posts publicly.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from openai_image_generator import generate_image_from_prompt


BASE_DIR = Path(__file__).resolve().parent

ANIME_VAULT_STYLE = (
    "BETGPTAI Anime Vault, 1080x1920 vertical premium anime daily MLB card, "
    "high-energy anime sports magazine, dark electric background, team mascot "
    "artwork, manga action panels, glowing borders, team-color lightning, premium "
    "ESPN x Topps x anime sports-card style, dramatic stadium lights, bold brush "
    "typography, clean mobile-readable layout"
)
NEGATIVE_STYLE = (
    "no emojis, no smiley faces, no placeholder icons, no flat infographic style"
)

SECTION_HEADINGS = {
    "play_of_day": "🔥 PLAY OF THE DAY",
    "moneyline": "🏆 TOP 2 MONEYLINE",
    "f5": "🔥 TOP 2 F5 MONEYLINE",
    "runline": "📈 TOP 2 RUNLINE/SPREAD",
    "totals": "🎯 TOP 2 OVER/UNDER TOTAL RUNS",
    "team_totals": "💰 TOP 2 TEAM TOTALS",
    "safe_parlay": "🧩 2-LEG SAFE PARLAY",
}


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: Any, fallback: str = "TBD") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _section(card: str, heading: str) -> str:
    start = card.find(heading)
    if start < 0:
        return ""
    end = len(card)
    for other in SECTION_HEADINGS.values():
        if other == heading:
            continue
        candidate = card.find(other, start + len(heading))
        if candidate >= 0:
            end = min(end, candidate)
    divider = card.find("━━━━━━━━━━━━", start + len(heading))
    if divider >= 0:
        end = min(end, divider)
    return card[start:end].strip()


def _strip_visual_noise(line: str) -> str:
    cleaned = re.sub(r"^[\s\d️⃣1-9.✅⚾🔥🏆📈🎯💰🧩⭐-]+", "", line).strip()
    cleaned = re.sub(
        r"^(Pick|Line|Risk Grade|Confidence Grade|Reason|Safer Line):\s*",
        "",
        cleaned,
        flags=re.I,
    )
    # Remove American odds display if any leaked through.
    cleaned = re.sub(r"\b[+-]\d{3,4}\b", "", cleaned).strip()
    return re.sub(r"\s+", " ", cleaned).strip()


def _picks(section: str, limit: int = 2) -> list[str]:
    skip_prefixes = (
        "reason", "line:", "risk grade", "confidence grade", "safer line",
        "parlay note", "singles", "parlays", "educational", "card timing",
        "odds vary", "please shop", "🆚", "🕒", "🔴", "📌",
    )
    found: list[str] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line or line == "━━━━━━━━━━━━" or line in SECTION_HEADINGS.values():
            continue
        if line.lower().startswith(skip_prefixes):
            continue
        cleaned = _strip_visual_noise(line)
        if not cleaned or cleaned.lower().startswith(skip_prefixes):
            continue
        if cleaned not in found:
            found.append(cleaned)
        if len(found) >= limit:
            break
    return found


def extract_mlb_auto_image_data(analysis: str) -> dict[str, list[str] | str]:
    """Extract the visual-safe top plays from a /mlb_auto text card."""
    data: dict[str, list[str] | str] = {}
    for key, heading in SECTION_HEADINGS.items():
        sec = _section(analysis, heading)
        limit = 1 if key == "play_of_day" else 3 if key == "safe_parlay" else 2
        values = _picks(sec, limit)
        data[key] = values[0] if key == "play_of_day" and values else values
    return data


def _list_text(label: str, values: Any) -> str:
    if isinstance(values, str):
        values = [values]
    values = values if isinstance(values, list) else []
    short = [_clean(value) for value in values[:3]]
    return f"{label}: " + ("; ".join(short) if short else "TBD")


def create_mlb_auto_prompt(analysis: str, card_date: str) -> str:
    """Create one Anime Vault prompt for the full free /mlb_auto card."""
    data = extract_mlb_auto_image_data(analysis)
    play = _clean(data.get("play_of_day"), "Play of the Day")
    sections = [
        _list_text("Play of the Day", [play]),
        _list_text("Top 2 Moneyline", data.get("moneyline")),
        _list_text("Top 2 F5", data.get("f5")),
        _list_text("Top 2 Runline", data.get("runline")),
        _list_text("Top 2 Totals", data.get("totals")),
        _list_text("Top 2 Team Totals", data.get("team_totals")),
        _list_text("Safe Parlay", data.get("safe_parlay")),
    ]

    return (
        f"{ANIME_VAULT_STYLE}. Create one official admin-preview image for the "
        f"free BETGPTAI /mlb_auto daily card. Format: 1080x1920 vertical. Use a "
        f"premium magazine/trading-card layout with the Play of the Day as the "
        f"largest hero panel and category panels for the remaining plays. Main "
        f"visual text must stay simple and must not show odds or sportsbook names. "
        f"Use these card sections: {' | '.join(sections)}. Include small labels "
        f"for Market, Game Time ET if available from the text, Confidence Grade or "
        f"gold stars, and one short reason area. Include disclaimer text: Singles "
        f"are recommended. Parlays carry greater risk. Educational analysis only. "
        f"Do not show AI disagreement, API names, raw model scores, or internal "
        f"rules. {NEGATIVE_STYLE}."
    )


def prepare_mlb_auto_image(
    analysis: str,
    card_date: str,
    *,
    output_root: str | Path | None = None,
    image_generation_enabled: bool | None = None,
) -> dict[str, Any]:
    """Save /mlb_auto image prompt and optionally generate the preview image."""
    output_base = Path(output_root) if output_root else BASE_DIR / "generated_cards"
    output_dir = output_base / card_date
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = create_mlb_auto_prompt(analysis, card_date)
    prompt_path = output_dir / "mlb_auto_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    image_path = output_dir / "mlb_auto_card.png"
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
        "data": extract_mlb_auto_image_data(analysis),
    }
