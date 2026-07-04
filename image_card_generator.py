"""Generate BETGPTAI Anime Vault visual cards with Pillow.

The scheduler uses this module to turn text/data cards into 1080x1920 social
media-ready posters. The design intentionally avoids a plain spreadsheet look:
dark electric background, neon accents, manga-style panels, glowing borders,
and optional anime mascot art when image files are available.
"""

from __future__ import annotations

import json
import math
import random
import re
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from storage import data_file


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = data_file("generated_cards")
DAILY_CARD_FILE = data_file("daily_card.json")
WIDTH, HEIGHT = 1080, 1920

NEON_YELLOW = (255, 235, 58)
NEON_BLUE = (0, 194, 255)
NEON_RED = (255, 49, 91)
NEON_GREEN = (0, 255, 153)
PANEL_DARK = (12, 16, 34)
TEXT = (245, 248, 255)
MUTED = (185, 198, 220)

SECTION_ALIASES = {
    "Best Bet": ("🔥 PLAY OF THE DAY", "🔥 BEST BET", "BEST BET"),
    "Best Moneyline": ("🏆 TOP 2 MONEYLINE", "TOP MONEYLINE", "MONEYLINE"),
    "Best Underdog": ("UNDERDOG", "VALUE PLAY"),
    "Best Runline": ("📈 TOP 2 RUNLINE/SPREAD", "RUNLINE", "SPREAD"),
    "Best Total": ("🎯 TOP 2 OVER/UNDER TOTAL RUNS", "OVER/UNDER", "TOTAL"),
    "Best Team Total": ("💰 TOP 2 TEAM TOTALS", "TEAM TOTAL"),
    "Safe Parlay": ("🧩 2-LEG SAFE PARLAY", "SAFE PARLAY"),
    "Value Parlay": ("VALUE PARLAY",),
    "Core Five Plays": ("CORE FIVE", "TOP 5", "FULL MLB CARD"),
}


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a bold system font, falling back to Pillow's default."""
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    if not bold:
        candidates = candidates[1::2] + candidates[::2]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _round_rect(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int, int] | tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
    width: int = 1,
) -> None:
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)


def _glow_line(
    image: Image.Image,
    start: tuple[int, int],
    end: tuple[int, int],
    color: tuple[int, int, int],
    width: int = 5,
) -> None:
    """Draw a neon line with a soft outer glow."""
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    for extra, alpha in ((18, 40), (10, 70), (4, 130)):
        draw.line([start, end], fill=(*color, alpha), width=width + extra)
    draw.line([start, end], fill=(*color, 255), width=width)
    image.alpha_composite(glow)


def _background() -> Image.Image:
    """Create the dark electric manga-poster background."""
    image = Image.new("RGBA", (WIDTH, HEIGHT), (5, 8, 22, 255))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            blue = int(18 + 35 * (x / WIDTH) + 25 * (y / HEIGHT))
            red = int(8 + 16 * (1 - y / HEIGHT))
            pixels[x, y] = (red, 10, blue, 255)

    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    random.seed(42)
    for _ in range(42):
        x = random.randint(-100, WIDTH + 100)
        y = random.randint(0, HEIGHT)
        length = random.randint(160, 460)
        color = random.choice([NEON_BLUE, NEON_RED, NEON_GREEN, NEON_YELLOW])
        draw.line(
            [(x, y), (x + length, y - random.randint(80, 240))],
            fill=(*color, random.randint(25, 70)),
            width=random.randint(3, 10),
        )
    image.alpha_composite(overlay.filter(ImageFilter.GaussianBlur(1.2)))

    # Manga action rays from the upper-right mascot area.
    rays = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(rays)
    origin = (WIDTH + 120, 300)
    for angle in range(150, 255, 5):
        rad = math.radians(angle)
        end = (
            int(origin[0] + math.cos(rad) * 1800),
            int(origin[1] + math.sin(rad) * 1800),
        )
        draw.line([origin, end], fill=(255, 255, 255, 18), width=3)
    image.alpha_composite(rays)
    return image


def _find_mascot() -> Path | None:
    """Find an anime mascot image when the user adds one later."""
    search_dirs = [
        BASE_DIR / "anime_vault",
        BASE_DIR / "assets",
        BASE_DIR / "mascots",
        BASE_DIR / "downloaded_files",
    ]
    patterns = ("*.png", "*.jpg", "*.jpeg", "*.webp")
    for folder in search_dirs:
        if not folder.exists():
            continue
        for pattern in patterns:
            for path in folder.glob(pattern):
                if any(word in path.stem.lower() for word in ("mascot", "anime", "reaper", "vault")):
                    return path
    return None


def _paste_mascot(image: Image.Image) -> None:
    """Place mascot art, or render a neon anime-style silhouette fallback."""
    mascot_path = _find_mascot()
    if mascot_path:
        try:
            mascot = Image.open(mascot_path).convert("RGBA")
            mascot.thumbnail((520, 720), Image.Resampling.LANCZOS)
            alpha = mascot.getchannel("A")
            glow = Image.new("RGBA", mascot.size, NEON_BLUE + (0,))
            glow.putalpha(alpha.filter(ImageFilter.GaussianBlur(18)))
            x, y = WIDTH - mascot.width - 25, 145
            image.alpha_composite(glow, (x, y))
            image.alpha_composite(mascot, (x, y))
            return
        except Exception:
            pass

    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    center = (835, 360)
    for radius, alpha in ((245, 24), (185, 38), (120, 70)):
        draw.ellipse(
            (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
            fill=(*NEON_BLUE, alpha),
        )
    draw.ellipse((710, 210, 960, 555), fill=(15, 20, 42, 230), outline=NEON_YELLOW, width=7)
    draw.polygon([(725, 295), (620, 255), (710, 360)], fill=(255, 49, 91, 210))
    draw.polygon([(945, 300), (1040, 255), (965, 360)], fill=(0, 255, 153, 200))
    draw.arc((760, 310, 910, 455), 10, 170, fill=NEON_YELLOW, width=9)
    image.alpha_composite(layer)


def _draw_header(image: Image.Image, title: str, subtitle: str) -> None:
    draw = ImageDraw.Draw(image)
    _glow_line(image, (40, 105), (650, 40), NEON_YELLOW, 6)
    draw.text((54, 60), "BETGPTAI", font=_font(82, True), fill=TEXT, stroke_width=4, stroke_fill=(0, 0, 0))
    draw.text((60, 145), "THE ODDS REAPER", font=_font(36, True), fill=NEON_YELLOW, stroke_width=2, stroke_fill=(0, 0, 0))
    _round_rect(draw, (55, 205, 665, 292), 24, (255, 49, 91, 225), outline=NEON_YELLOW, width=4)
    draw.text((82, 222), title.upper()[:28], font=_font(45, True), fill=TEXT)
    draw.text((84, 270), subtitle[:48], font=_font(25, True), fill=(255, 245, 180))


def _draw_vault_header(image: Image.Image) -> None:
    """Draw the high-energy THE VAULT header from the BETGPTAI reference style."""
    draw = ImageDraw.Draw(image)
    draw.text((28, 30), "BET", font=_font(58, True), fill=TEXT, stroke_width=3, stroke_fill=(0, 0, 0))
    draw.text((132, 30), "GPT", font=_font(58, True), fill=NEON_RED, stroke_width=3, stroke_fill=(0, 0, 0))
    draw.text((256, 30), "AI", font=_font(58, True), fill=TEXT, stroke_width=3, stroke_fill=(0, 0, 0))
    draw.text((62, 98), "THE ODDS REAPER", font=_font(24, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))
    draw.text((385, 32), "OFFICIAL MLB CARD", font=_font(34, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))
    draw.text((300, 82), "THE VAULT", font=_font(112, True), fill=NEON_YELLOW, stroke_width=5, stroke_fill=(40, 18, 0))
    draw.text((430, 205), "STACK EDGES, NOT EMOTIONS.", font=_font(34, True), fill=TEXT, stroke_width=3, stroke_fill=(0, 0, 0))
    _glow_line(image, (20, 245), (1040, 245), NEON_YELLOW, 4)


def _draw_anime_badge(
    image: Image.Image,
    box: tuple[int, int, int, int],
    accent: tuple[int, int, int],
    label: str = "",
) -> None:
    """Draw a small anime mascot placeholder when no uploaded art exists."""
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    radius = min(x2 - x1, y2 - y1) // 2
    for grow, alpha in ((28, 35), (12, 80)):
        draw.ellipse((cx - radius - grow, cy - radius - grow, cx + radius + grow, cy + radius + grow), fill=(*accent, alpha))
    draw.ellipse((x1, y1, x2, y2), fill=(15, 20, 42, 235), outline=accent, width=5)
    draw.pieslice((x1 + 12, y1 + 18, x2 - 12, y2 + 100), 180, 360, fill=accent)
    draw.rectangle((x1 + 22, y1 + 58, x2 - 22, y1 + 85), fill=accent)
    draw.ellipse((cx - 56, cy - 35, cx + 56, cy + 75), fill=(244, 176, 92, 255), outline=(0, 0, 0), width=3)
    draw.ellipse((cx - 34, cy - 2, cx - 16, cy + 18), fill=TEXT)
    draw.ellipse((cx + 16, cy - 2, cx + 34, cy + 18), fill=TEXT)
    draw.ellipse((cx - 26, cy + 4, cx - 16, cy + 16), fill=(0, 0, 0))
    draw.ellipse((cx + 16, cy + 4, cx + 26, cy + 16), fill=(0, 0, 0))
    draw.arc((cx - 34, cy + 22, cx + 34, cy + 58), 0, 180, fill=(0, 0, 0), width=4)
    if label:
        draw.text((x1 + 12, y2 - 40), label[:8].upper(), font=_font(22, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))
    image.alpha_composite(layer)


def _draw_action_panel(
    image: Image.Image,
    box: tuple[int, int, int, int],
    label: str,
    body: str,
    accent: tuple[int, int, int],
    mascot: bool = True,
) -> None:
    """Draw a comic-style betting panel with hard neon hierarchy."""
    x1, y1, x2, y2 = box
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for grow, alpha in ((18, 40), (8, 90)):
        _round_rect(draw, (x1 - grow, y1 - grow, x2 + grow, y2 + grow), 10 + grow, (*accent, alpha))
    _round_rect(draw, box, 10, (6, 9, 22, 238), outline=accent, width=4)
    draw.rectangle((x1, y1, x1 + 9, y2), fill=accent)
    image.alpha_composite(layer)
    draw = ImageDraw.Draw(image)
    draw.text((x1 + 24, y1 + 14), label.upper(), font=_font(26, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))
    lines = _wrap(body, 18 if mascot else 26)
    current = y1 + 52
    for idx, line in enumerate(lines[:3]):
        fill = accent if idx == 0 else TEXT
        draw.text((x1 + 24, current), line.upper(), font=_font(38 if idx == 0 else 30, True), fill=fill, stroke_width=2, stroke_fill=(0, 0, 0))
        current += 42
    if mascot:
        _draw_anime_badge(image, (x2 - 142, y1 + 12, x2 - 18, y2 - 12), accent)


def _draw_big_best_bet(image: Image.Image, body: str) -> None:
    """Draw the large left hero panel for Best Bet."""
    x1, y1, x2, y2 = 35, 285, 590, 760
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for grow, alpha in ((25, 40), (10, 95)):
        _round_rect(draw, (x1 - grow, y1 - grow, x2 + grow, y2 + grow), 14 + grow, (*NEON_YELLOW, alpha))
    _round_rect(draw, (x1, y1, x2, y2), 14, (5, 8, 18, 238), outline=NEON_YELLOW, width=5)
    image.alpha_composite(layer)
    _draw_anime_badge(image, (55, 330, 310, 585), NEON_YELLOW, "VAULT")
    draw = ImageDraw.Draw(image)
    draw.text((330, 330), "BEST BET", font=_font(42, True), fill=NEON_YELLOW, stroke_width=3, stroke_fill=(0, 0, 0))
    confidence_match = re.search(r"(\d+(?:\.\d+)?/10)", body)
    display_body = re.sub(r"\b\d+(?:\.\d+)?/10\b", "", body).strip()
    lines = _wrap(display_body, 13)
    current = 395
    for idx, line in enumerate(lines[:4]):
        size = 50 if idx < 2 else 42
        fill = NEON_YELLOW if idx == len(lines[:4]) - 1 else TEXT
        draw.text((330, current), line.upper(), font=_font(size, True), fill=fill, stroke_width=3, stroke_fill=(0, 0, 0))
        current += size + 8
    draw.text((360, 620), confidence_match.group(1) if confidence_match else "9/10", font=_font(92, True), fill=NEON_YELLOW, stroke_width=4, stroke_fill=(0, 0, 0))
    draw.text((58, 704), "CLEANEST ATTACK SPOT!", font=_font(34, True), fill=NEON_YELLOW, stroke_width=2, stroke_fill=(0, 0, 0))


def _draw_parlay_box(
    image: Image.Image,
    box: tuple[int, int, int, int],
    label: str,
    body: str,
    accent: tuple[int, int, int],
) -> None:
    x1, y1, x2, y2 = box
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    _round_rect(draw, box, 10, (6, 9, 20, 238), outline=accent, width=4)
    draw.text((x1 + 18, y1 + 14), label.upper(), font=_font(35, True), fill=TEXT, stroke_width=3, stroke_fill=(0, 0, 0))
    image.alpha_composite(layer)
    draw = ImageDraw.Draw(image)
    parts = [line.strip() for line in str(body or "").splitlines() if line.strip()][:3]
    slot_w = max(1, (x2 - x1 - 36) // max(1, len(parts)))
    for idx, part in enumerate(parts):
        sx = x1 + 18 + idx * slot_w
        _draw_anime_badge(image, (sx, y1 + 70, sx + 105, y1 + 175), accent)
        draw.text((sx + 8, y1 + 184), part.upper()[:10], font=_font(22, True), fill=accent, stroke_width=2, stroke_fill=(0, 0, 0))
        if idx < len(parts) - 1:
            draw.text((sx + slot_w - 42, y1 + 104), "+", font=_font(60, True), fill=TEXT, stroke_width=3, stroke_fill=(0, 0, 0))


def _draw_core_five(image: Image.Image, body: str) -> None:
    x1, y1, x2, y2 = 540, 1018, 1045, 1408
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    _round_rect(draw, (x1, y1, x2, y2), 12, (7, 9, 21, 240), outline=NEON_YELLOW, width=5)
    draw.text((x1 + 70, y1 + 18), "CORE FIVE PLAYS", font=_font(34, True), fill=NEON_YELLOW, stroke_width=2, stroke_fill=(0, 0, 0))
    image.alpha_composite(layer)
    draw = ImageDraw.Draw(image)
    lines = [line.strip() for line in str(body or "").splitlines() if line.strip()][:5]
    while len(lines) < 5:
        lines.append("Vault play loading")
    y = y1 + 74
    colors = [NEON_YELLOW, NEON_BLUE, NEON_RED, NEON_BLUE, NEON_YELLOW]
    for idx, line in enumerate(lines[:5], start=1):
        draw.rectangle((x1 + 22, y - 6, x2 - 22, y + 47), fill=(12, 18, 38), outline=colors[idx - 1], width=2)
        draw.text((x1 + 34, y), str(idx), font=_font(38, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))
        draw.text((x1 + 88, y + 5), line.upper()[:24], font=_font(27, True), fill=TEXT, stroke_width=1, stroke_fill=(0, 0, 0))
        y += 60


def _draw_bottom_slogan(image: Image.Image) -> None:
    draw = ImageDraw.Draw(image)
    y = 1468
    labels = [("R", "RESEARCH"), ("D", "DISCIPLINE"), ("B", "BANKROLL"), ("C", "CONSISTENCY")]
    x = 45
    for icon, label in labels:
        _round_rect(draw, (x, y, x + 235, y + 95), 8, (8, 12, 24, 220), outline=NEON_YELLOW, width=2)
        draw.text((x + 20, y + 20), icon, font=_font(38, True), fill=NEON_YELLOW)
        draw.text((x + 62, y + 18), label, font=_font(22, True), fill=TEXT)
        draw.text((x + 62, y + 48), "Stick to the plan", font=_font(18, True), fill=MUTED)
        x += 245
    draw.text((45, 1605), "WE BET NUMBERS.", font=_font(58, True), fill=TEXT, stroke_width=4, stroke_fill=(0, 0, 0))
    draw.text((45, 1680), "YOU CASH TICKETS.", font=_font(76, True), fill=NEON_YELLOW, stroke_width=5, stroke_fill=(0, 0, 0))
    draw.text((170, 1770), "RESEARCH  -  DISCIPLINE  -  BANKROLL  -  REPEAT", font=_font(26, True), fill=TEXT, stroke_width=2, stroke_fill=(0, 0, 0))


def _draw_vault_mlb_layout(image: Image.Image, sections: dict[str, Any]) -> None:
    """Draw the reference-inspired Anime Vault MLB poster layout."""
    _draw_vault_header(image)
    _draw_big_best_bet(image, str(sections.get("Best Bet") or "Best bet loading"))
    side = [
        ("BEST MONEYLINE", str(sections.get("Best Moneyline") or "Moneyline loading"), NEON_BLUE),
        ("BEST UNDERDOG", str(sections.get("Best Underdog") or "Value dog loading"), NEON_YELLOW),
        ("BEST RUNLINE", str(sections.get("Best Runline") or "Runline loading"), NEON_BLUE),
        ("BEST TOTAL", str(sections.get("Best Total") or "Total loading"), NEON_RED),
        ("BEST TEAM TOTAL", str(sections.get("Best Team Total") or "Team total loading"), NEON_GREEN),
    ]
    y = 285
    for label, body, accent in side:
        _draw_action_panel(image, (610, y, 1045, y + 132), label, body, accent)
        y += 145
    _draw_parlay_box(image, (35, 820, 520, 1065), "Safe Parlay", str(sections.get("Safe Parlay") or "Safe parlay loading"), NEON_GREEN)
    _draw_parlay_box(image, (35, 1092, 520, 1348), "Value Parlay", str(sections.get("Value Parlay") or "Value parlay loading"), (208, 78, 255))
    _draw_core_five(image, str(sections.get("Core Five Plays") or "\n".join(
        str(sections.get(key)) for key in ("Best Bet", "Best Moneyline", "Best Runline", "Best Total", "Best Team Total") if sections.get(key)
    )))
    _draw_anime_badge(image, (745, 1395, 1035, 1685), NEON_BLUE, "REAPER")
    _draw_bottom_slogan(image)


def _wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").splitlines() or [""]:
        if not raw.strip():
            lines.append("")
        else:
            lines.extend(textwrap.wrap(raw.strip(), width=width))
    return lines


def _draw_panel(
    image: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    body: str,
    accent: tuple[int, int, int],
) -> int:
    """Draw one glowing manga panel and return the next y-position."""
    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    for spread, alpha in ((20, 35), (9, 75)):
        _round_rect(draw, (x - spread, y - spread, x + w + spread, y + h + spread), 28 + spread, (*accent, alpha))
    _round_rect(draw, (x, y, x + w, y + h), 28, (10, 14, 31, 232), outline=accent, width=4)
    draw.rectangle((x, y, x + 15, y + h), fill=accent)
    image.alpha_composite(panel)

    draw = ImageDraw.Draw(image)
    draw.text((x + 34, y + 20), label.upper(), font=_font(31, True), fill=accent, stroke_width=1, stroke_fill=(0, 0, 0))
    current = y + 68
    for line in _wrap(body, 34)[:5]:
        draw.text((x + 34, current), line, font=_font(34, True), fill=TEXT)
        current += 42
    return y + h + 22


def _clean_line(line: str) -> str:
    """Remove Telegram-specific noise while preserving market numbers."""
    cleaned = re.sub(r"^[✅⚾🔥🏆📈🎯💰🧩🌍⚽📊💎🥇🥈🥉1️⃣2️⃣3️⃣4️⃣5️⃣\-\s]+", "", line).strip()
    cleaned = re.sub(r"(?i)^line:\s*[+-]?\d+(?:\.\d+)?\s*$", "", cleaned)
    cleaned = re.sub(r"(?i)^risk grade:\s*", "Confidence: ", cleaned)
    cleaned = re.sub(r"(?i)^🎯 confidence grade:\s*", "Confidence: ", cleaned)
    cleaned = re.sub(r"(?i)^reason:\s*", "", cleaned)
    return cleaned.strip()


def _section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    end = text.find("━━━━━━━━━━━━", start + len(heading))
    return text[start + len(heading): end if end >= 0 else len(text)].strip()


def _first_meaningful_line(block: str) -> str:
    for line in block.splitlines():
        cleaned = _clean_line(line)
        if cleaned and not cleaned.startswith(("🆚", "🕒", "Confidence:", "Safer Line")):
            return cleaned
    return "Card loading"


def _extract_sections_from_text(text: str) -> dict[str, str]:
    """Map a Telegram text card into visual card sections."""
    sections: dict[str, str] = {}
    for label, headings in SECTION_ALIASES.items():
        for heading in headings:
            block = _section(text, heading)
            if block:
                lines = [_clean_line(line) for line in block.splitlines()]
                lines = [line for line in lines if line]
                if lines:
                    sections[label] = "\n".join(lines[:4])
                break
    if "Best Bet" not in sections:
        sections["Best Bet"] = _first_meaningful_line(text)
    return sections


def load_daily_card() -> dict[str, Any]:
    """Read daily_card.json if present; return an empty payload otherwise."""
    try:
        payload = json.loads(DAILY_CARD_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def save_daily_card_payload(card_type: str, text: str, day: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Persist the latest generated card for visual rendering and auditing."""
    payload = load_daily_card()
    cards = payload.setdefault("cards", {})
    card_payload = {
        "type": card_type,
        "date": day,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sections": _extract_sections_from_text(text),
        "raw_text": text,
    }
    if extra:
        card_payload.update(extra)
    cards[card_type] = card_payload
    payload["latest_type"] = card_type
    payload["latest_date"] = day
    temporary = DAILY_CARD_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(DAILY_CARD_FILE)
    return card_payload


def _payload_for(card_type: str, text: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve data from direct payload, daily_card.json, or text fallback."""
    if payload:
        return payload
    daily = load_daily_card()
    cards = daily.get("cards", {}) if isinstance(daily.get("cards"), dict) else {}
    if card_type in cards and isinstance(cards[card_type], dict):
        return cards[card_type]
    if text:
        return {"type": card_type, "sections": _extract_sections_from_text(text), "raw_text": text}
    return {"type": card_type, "sections": {"Best Bet": "Today's card is being prepared."}}


def _title_for(card_type: str) -> tuple[str, str]:
    mapping = {
        "play": ("Play of the Day", "Anime Vault Free Drop"),
        "free_mlb": ("MLB Free Card", "Pregame edges • Singles first"),
        "full_mlb": ("Full MLB Vault", "Premium slate attack"),
        "f5": ("F5 Card", "Starting pitcher battle"),
        "team_totals": ("Team Totals", "Run environment angles"),
        "nrfi": ("NRFI Vault", "First-inning pressure"),
        "soccer": ("Soccer Card", "Global football edges"),
        "worldcup": ("World Cup Card", "Every match • Tournament mode"),
        "results": ("Results Tracker", "Records • Units • Momentum"),
        "vip": ("VIP Membership", "Unlock The Vault"),
    }
    return mapping.get(card_type, ("BETGPTAI Card", "The Odds Reaper"))


def generate_card_image(
    card_type: str,
    *,
    text: str | None = None,
    payload: dict[str, Any] | None = None,
    output_path: str | Path | None = None,
) -> Path:
    """Generate a 1080x1920 BETGPTAI Anime Vault poster and return its path.

    Disabled on purpose: placeholder/Pillow-only cards do not meet the Anime
    Vault standard. Use /mlb_images to produce detailed prompts for real artwork
    first, then reintroduce Pillow later only for text placement.
    """
    raise RuntimeError(
        "Pillow final-card generation is disabled. Use /mlb_images for "
        "BETGPTAI Anime Vault image prompts."
    )
    OUTPUT_DIR.mkdir(exist_ok=True)
    data = _payload_for(card_type, text=text, payload=payload)
    sections = data.get("sections") if isinstance(data.get("sections"), dict) else {}
    title, subtitle = _title_for(card_type)
    image = _background()

    if card_type in {"play", "free_mlb", "full_mlb", "f5", "team_totals", "nrfi", "test_mlb"}:
        _draw_vault_mlb_layout(image, sections)
        image = ImageEnhance.Contrast(image.convert("RGB")).enhance(1.12)
        if output_path is None:
            safe_type = re.sub(r"[^a-z0-9_]+", "_", card_type.lower())
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = OUTPUT_DIR / f"{safe_type}_{stamp}.png"
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, "PNG", optimize=True)
        return output

    _paste_mascot(image)
    _draw_header(image, title, subtitle)

    draw = ImageDraw.Draw(image)
    draw.text((64, 330), "THE VAULT CARD", font=_font(34, True), fill=NEON_GREEN)
    draw.text((64, 368), str(data.get("date") or datetime.now().strftime("%m/%d/%Y")), font=_font(25, True), fill=MUTED)

    labels = [
        "Best Bet", "Best Moneyline", "Best Underdog", "Best Runline",
        "Best Total", "Best Team Total", "Safe Parlay", "Value Parlay",
        "Core Five Plays",
    ]
    if card_type == "results":
        labels = ["Best Bet", "Core Five Plays", "Safe Parlay", "Value Parlay"]
    if card_type in {"soccer", "worldcup"}:
        labels = ["Best Bet", "Best Moneyline", "Best Total", "Safe Parlay", "Core Five Plays"]
    if card_type == "vip":
        labels = ["Best Bet", "Core Five Plays", "Safe Parlay"]

    accents = [NEON_YELLOW, NEON_BLUE, NEON_GREEN, NEON_RED]
    y = 425
    for index, label in enumerate(labels):
        body = str(sections.get(label) or "").strip()
        if not body:
            continue
        height = 168 if label != "Core Five Plays" else 238
        if y + height > 1760:
            break
        y = _draw_panel(image, 58, y, 964, height, label, body, accents[index % len(accents)])

    # Footer ribbon.
    ribbon = Image.new("RGBA", image.size, (0, 0, 0, 0))
    rdraw = ImageDraw.Draw(ribbon)
    _round_rect(rdraw, (58, 1780, 1022, 1868), 24, (0, 0, 0, 185), outline=NEON_YELLOW, width=3)
    rdraw.text((85, 1800), "SINGLES FIRST • PARLAYS OPTIONAL • PLAY RESPONSIBLY", font=_font(30, True), fill=TEXT)
    rdraw.text((85, 1838), "Visual-first BETGPTAI Anime Vault card", font=_font(22, True), fill=NEON_YELLOW)
    image.alpha_composite(ribbon)

    # Final contrast pop.
    image = ImageEnhance.Contrast(image.convert("RGB")).enhance(1.08)
    if output_path is None:
        safe_type = re.sub(r"[^a-z0-9_]+", "_", card_type.lower())
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = OUTPUT_DIR / f"{safe_type}_{stamp}.png"
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)
    return output


def short_caption(card_type: str) -> str:
    """Keep Telegram image captions compact."""
    title, _ = _title_for(card_type)
    return f"BETGPTAI Anime Vault — {title}\nThe Odds Reaper"


def sample_mlb_card_payload() -> dict[str, Any]:
    """Return fake test picks for the owner-only image preview command."""
    return {
        "type": "test_mlb",
        "date": datetime.now().strftime("%m/%d/%Y"),
        "sections": {
            "Best Bet": "Brewers Team Total\nOver 4.5\n9/10",
            "Best Moneyline": "Cubs ML",
            "Best Underdog": "Padres +1.5",
            "Best Runline": "Brewers -1.5\n+102",
            "Best Total": "Red Sox / Rockies\nOver 11.5",
            "Best Team Total": "Brewers\nOver 4.5",
            "Safe Parlay": "Brewers TT Over 4.5\nCubs ML",
            "Value Parlay": "Brewers -1.5 +102\nRed Sox/Rockies Over 11.5\nPadres +1.5",
            "Core Five Plays": (
                "Brewers Team Total Over 4.5\n"
                "Cubs ML\n"
                "Brewers ML\n"
                "Red Sox / Rockies Over 11.5\n"
                "Brewers -1.5 +102"
            ),
        },
        "raw_text": "Owner-only fake sample card for visual testing.",
    }


def generate_test_mlb_card(output_path: str | Path | None = None) -> Path:
    """Create the owner-only fake MLB test card as test_mlb_card.png."""
    return generate_card_image(
        "test_mlb",
        payload=sample_mlb_card_payload(),
        output_path=output_path or (data_file("generated_cards") / "test_mlb_card.png"),
    )

def generate_mlb_card_slides(card_data: dict) -> list[str]:
    """
    Compatibility wrapper for MLB Anime Vault prompt slides.
    Returns 7 ready-to-copy image prompts.
    """
    from card_image_generator import generate_mlb_card_slides as generator
    return generator(card_data)
