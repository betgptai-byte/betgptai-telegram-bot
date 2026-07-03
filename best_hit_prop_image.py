"""BEST HIT PROP OF THE DAY Anime Vault V2 image workflow.

V2 separates artwork from card layout:

1. Verify the player and matchup.
2. Ask OpenAI Images for artwork only: no text, no logos, no typography.
3. Compose the final 1080x1920 card with Pillow so every card has the same
   premium magazine layout and reliable text.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from openai_image_generator import generate_image
from player_props_engine import build_player_props_lab
from player_verification import verify_player_team_by_id
from team_colors import get_team_colors


BASE_DIR = Path(__file__).resolve().parent
CARD_SIZE = (1080, 1920)


def _truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _display_folder(card_date: str) -> str:
    """Return MM-DD-YYYY folder naming requested for final promotional assets."""
    try:
        return datetime.fromisoformat(card_date).strftime("%m-%d-%Y")
    except ValueError:
        return card_date


def _clean(value: Any, fallback: str = "Unavailable") -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text or fallback


def _format_player_name(value: Any) -> str:
    """Always display FirstName LastName, never LastName, FirstName."""
    name = _clean(value, "Player")
    if "," in name:
        last, first = [part.strip() for part in name.split(",", 1)]
        if first and last:
            return f"{first} {last}"
    return name


def _prop_text(prop: dict[str, Any]) -> str:
    line = prop.get("line")
    return f"Over {line} Hits" if line is not None else "Over 0.5 Hits"


def _short_bullets(prop: dict[str, Any]) -> list[str]:
    reason = str(prop.get("reason") or "")
    savant = prop.get("savant_verification") or {}
    bullets: list[str] = []
    lineup = prop.get("lineup_verification") or {}
    spot = lineup.get("lineup_spot")
    if isinstance(spot, int) and spot <= 5:
        bullets.append(f"Projected top-{spot} lineup spot")
    else:
        bullets.append("Strong lineup role")
    if savant.get("verified"):
        bullets.append("Favorable Statcast matchup")
    if any(word in reason.lower() for word in ("contact", "hardhit", "xba", "xwoba")):
        bullets.append("Strong recent contact profile")
    if len(bullets) < 3:
        bullets.append("Verified team and matchup context")
    return bullets[:3]


def _savant_verified(prop: dict[str, Any]) -> bool:
    savant = prop.get("savant_verification") or {}
    if not savant.get("verified"):
        return False
    required = ("xBA", "hard_hit_pct", "barrel_pct")
    return any(savant.get(key) not in (None, "", "unavailable") for key in required)


def _matchup_verified(prop: dict[str, Any], slate: list[dict[str, Any]]) -> dict[str, Any]:
    game_pk = prop.get("game_pk")
    team = prop.get("team_name") or prop.get("team")
    opponent = prop.get("opponent_name") or prop.get("opponent")
    for game in slate:
        if game_pk and game.get("game_id") != game_pk and game.get("game_pk") != game_pk:
            continue
        teams = {game.get("away_team"), game.get("home_team")}
        if team in teams and opponent in teams:
            return {
                "verified": True,
                "status": "verified_today_matchup",
                "game_pk": game.get("game_id") or game.get("game_pk"),
                "game_time": game.get("game_time"),
                "reason": f"{team} plays {opponent} today.",
            }
    return {
        "verified": False,
        "status": "matchup_not_found",
        "reason": f"Could not confirm {team} vs {opponent} on today's MLB slate.",
    }


def _lineup_verified(prop: dict[str, Any]) -> bool:
    lineup = prop.get("lineup_verification") or {}
    return bool(lineup.get("verified"))


def _verification_result(prop: dict[str, Any], slate: list[dict[str, Any]]) -> dict[str, Any]:
    """Run all V2 checks for one hit prop."""
    player_id = prop.get("player_id")
    expected_team = prop.get("team_name") or prop.get("team")
    player_check = verify_player_team_by_id(player_id, str(expected_team or ""))
    matchup_check = _matchup_verified(prop, slate)
    savant_ok = _savant_verified(prop)
    lineup_ok = _lineup_verified(prop)
    verified = (
        bool(player_check.get("verified"))
        and bool(matchup_check.get("verified"))
        and savant_ok
        and lineup_ok
        and bool(player_id)
    )
    reasons = []
    if not player_check.get("verified"):
        reasons.append(str(player_check.get("reason")))
    if not matchup_check.get("verified"):
        reasons.append(str(matchup_check.get("reason")))
    if not savant_ok:
        reasons.append("Baseball Savant player ID/contact metrics were not confirmed.")
    if not lineup_ok:
        reasons.append("Projected or confirmed lineup could not be confirmed.")
    return {
        "verified": verified,
        "player_id": player_id,
        "current_team": player_check.get("current_team"),
        "player_name": player_check.get("player_name") or prop.get("player_name"),
        "matchup": matchup_check,
        "savant_verified": savant_ok,
        "lineup_verified": lineup_ok,
        "reason": "All V2 checks passed." if verified else " ".join(reasons),
    }


def select_best_hit_prop(
    payload: dict[str, Any],
    slate: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Select the highest-rated hit prop that passes every V2 verification."""
    rejections: list[str] = []
    for prop in payload.get("candidates", {}).get("hits", []):
        check = _verification_result(prop, slate)
        prop["image_v2_verification"] = check
        if check.get("verified"):
            prop["player_name"] = _format_player_name(check.get("player_name") or prop.get("player_name"))
            prop["team_name"] = check.get("current_team") or prop.get("team_name")
            return prop, rejections
        rejections.append(f"{prop.get('player_name')}: {check.get('reason')}")
    return None, rejections


def create_artwork_prompt(prop: dict[str, Any]) -> str:
    """Prompt image generation for artwork only: no rendered card text."""
    player = _format_player_name(prop.get("player_name"))
    team = _clean(prop.get("team_name"), "Verified Team")
    colors = get_team_colors(team)
    return (
        "Anime baseball hero, verified current team uniform colors for "
        f"{team}, use only this verified team palette: {colors['names']} "
        f"({colors['primary']}, {colors['secondary']}, {colors['accent']}), "
        f"dynamic batting action pose inspired by {player}, electric lightning "
        "in the verified team colors, manga speed lines, hyper-detailed cel "
        "shading, cinematic baseball stadium, premium sports-card illustration, dramatic rim light, "
        "intense athletic expression, collectible anime magazine cover art, "
        "vertical portrait composition, no text, no logos, no typography, no "
        "letters, no numbers, no watermark, no scoreboard text. Never use "
        "generic random colors."
    )


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int] = (255, 255, 255),
    stroke_width: int = 0,
    stroke_fill: tuple[int, int, int] = (0, 0, 0),
) -> None:
    draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    probe = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(probe)
    for word in words:
        candidate = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = image.convert("RGB")
    ratio = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize((int(image.width * ratio), int(image.height * ratio)), Image.Resampling.LANCZOS)
    left = (resized.width - size[0]) // 2
    top = (resized.height - size[1]) // 2
    return resized.crop((left, top, left + size[0], top + size[1]))


def compose_best_hit_card(prop: dict[str, Any], art_path: str | Path, output_path: str | Path) -> str:
    """Compose the final consistent 1080x1920 BETGPTAI card with Pillow."""
    colors = get_team_colors(prop.get("team_name"))
    primary = colors["primary_rgb"]
    secondary = colors["secondary_rgb"]
    accent = colors["accent_rgb"]
    art = _fit_cover(Image.open(art_path), CARD_SIZE)
    art = ImageEnhance.Color(art).enhance(1.2)
    art = ImageEnhance.Contrast(art).enhance(1.15)

    # Dark cinematic overlays for consistent text readability.
    overlay = Image.new("RGBA", CARD_SIZE, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rectangle((0, 0, 1080, 270), fill=(0, 0, 0, 170))
    od.rectangle((0, 1180, 1080, 1920), fill=(0, 0, 0, 195))
    od.rectangle((700, 430, 1050, 760), fill=(0, 0, 0, 125))
    # Team-color gradient glow behind the layout panels.
    for index in range(0, 1080, 18):
        alpha = int(70 * (index / 1080))
        od.line((index, 0, index, 1920), fill=(*primary, alpha), width=18)
    od.ellipse((-260, 980, 520, 1760), fill=(*secondary, 65))
    od.ellipse((650, 250, 1320, 920), fill=(*primary, 65))
    art = Image.alpha_composite(art.convert("RGBA"), overlay)

    card = Image.new("RGBA", CARD_SIZE, (5, 8, 18, 255))
    card.alpha_composite(art)
    draw = ImageDraw.Draw(card)

    gold = accent if sum(accent) < 700 else (255, 214, 48)
    white = (255, 255, 255)
    red = primary
    blue = secondary
    green = (91, 255, 129)

    # Outer glowing borders and panels.
    for width, color in ((10, primary), (5, secondary), (2, accent)):
        draw.rounded_rectangle((28, 28, 1052, 1892), radius=32, outline=color, width=width)
    draw.rounded_rectangle((48, 1210, 1032, 1665), radius=28, fill=(0, 0, 0, 205), outline=primary, width=4)
    draw.rounded_rectangle((48, 1688, 1032, 1852), radius=24, fill=(0, 0, 0, 205), outline=secondary, width=3)
    draw.rounded_rectangle((720, 455, 1018, 700), radius=26, fill=(0, 0, 0, 185), outline=gold, width=4)

    # Header.
    _draw_text(draw, (64, 58), "BETGPTAI", _font(82, True), white, 3, (0, 0, 0))
    _draw_text(draw, (66, 148), "HIT PROP OF THE DAY", _font(56, True), gold, 2, (0, 0, 0))
    _draw_text(draw, (68, 214), "THE ODDS REAPER", _font(28, True), red, 1, (0, 0, 0))

    # Elite badge.
    _draw_text(draw, (765, 488), "ELITE", _font(48, True), gold, 2, (0, 0, 0))
    _draw_text(draw, (760, 550), "PLAY", _font(48, True), white, 2, (0, 0, 0))
    _draw_text(draw, (758, 618), "★★★★", _font(44, True), gold, 1, (0, 0, 0))

    player = _format_player_name(prop.get("player_name"))
    team = _clean(prop.get("team_name"))
    opponent = _clean(prop.get("opponent_name"))
    time = _clean(prop.get("game_time_et"), "Time unavailable ET")
    prop_text = _prop_text(prop)

    y = 1240
    label_font = _font(28, True)
    value_font = _font(43, True)
    for label, value in (
        ("PLAYER", player),
        ("CURRENT TEAM", team),
        ("OPPONENT", opponent),
        ("GAME TIME ET", time),
    ):
        _draw_text(draw, (80, y), label, label_font, blue, 1, (0, 0, 0))
        _draw_text(draw, (80, y + 34), value, value_font, white, 2, (0, 0, 0))
        y += 94

    _draw_text(draw, (615, 1242), "PROP", _font(34, True), green, 1, (0, 0, 0))
    for index, line in enumerate(_wrap_text(prop_text, _font(55, True), 360)[:2]):
        _draw_text(draw, (615, 1290 + index * 62), line, _font(55, True), gold, 2, (0, 0, 0))

    _draw_text(draw, (80, 1518), "WHY WE LIKE IT", _font(34, True), gold, 1, (0, 0, 0))
    bullet_font = _font(34, True)
    for index, bullet in enumerate(_short_bullets(prop), start=0):
        _draw_text(draw, (92, 1570 + index * 48), f"• {bullet}", bullet_font, white, 1, (0, 0, 0))

    _draw_text(draw, (78, 1716), "BETGPTAI", _font(58, True), white, 2, (0, 0, 0))
    _draw_text(draw, (78, 1780), "The Odds Reaper", _font(34, True), gold, 1, (0, 0, 0))
    footer_font = _font(24, False)
    _draw_text(draw, (455, 1722), "Educational analysis only.", footer_font, white)
    _draw_text(draw, (455, 1760), "Singles are recommended.", footer_font, white)
    _draw_text(draw, (455, 1798), "Parlays carry greater risk.", footer_font, white)

    # Subtle final sharpen.
    card = card.convert("RGB").filter(ImageFilter.SHARPEN)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    card.save(output, "PNG")
    return str(output)


def prepare_best_hit_prop_image(
    slate: list[dict[str, Any]],
    card_date: str,
    *,
    output_root: str | Path | None = None,
    image_generation_enabled: bool | None = None,
) -> dict[str, Any]:
    """Build, verify, generate artwork, and compose the final Best Hit card."""
    output_base = Path(output_root) if output_root else BASE_DIR / "generated_cards"
    output_dir = output_base / _display_folder(card_date)
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = build_player_props_lab(slate, card_date)
    prop, rejections = select_best_hit_prop(payload, slate)
    if not prop:
        return {
            "status": "no_verified_prop",
            "reason": "No hit prop passed V2 player/team/matchup/lineup/Savant verification.",
            "rejections": rejections,
            "payload": payload,
        }

    prompt = create_artwork_prompt(prop)
    prompt_path = output_dir / "best_hit_art_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    art_path = output_dir / "best_hit_art.png"
    final_path = output_dir / "best_hit_prop.png"
    image_error = None

    enabled = _truthy_env("IMAGE_GENERATION_ENABLED") if image_generation_enabled is None else image_generation_enabled
    if enabled:
        try:
            generate_image(prompt, str(art_path))
            compose_best_hit_card(prop, art_path, final_path)
            print(f"Image successfully created:\n{final_path}", flush=True)
        except Exception as error:
            image_error = str(error)

    return {
        "status": "ready",
        "version": "Anime Vault V2",
        "prop": prop,
        "prompt": prompt,
        "prompt_path": str(prompt_path),
        "art_path": str(art_path) if art_path.exists() else None,
        "image_path": str(final_path) if final_path.exists() else None,
        "image_error": image_error,
        "rejections": rejections,
        "payload": payload,
    }
