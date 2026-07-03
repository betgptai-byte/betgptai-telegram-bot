"""Official-ish MLB team color palettes for BETGPTAI Anime Vault cards.

These palettes are used for prompts and Pillow composition so image cards use
the verified player's current team colors instead of random generic colors.
"""

from __future__ import annotations

from typing import Any


MLB_TEAM_COLORS: dict[str, dict[str, Any]] = {
    "Arizona Diamondbacks": {"primary": "#A71930", "secondary": "#30CED8", "accent": "#E3D4AD", "names": "sedona red, teal, sand"},
    "Athletics": {"primary": "#003831", "secondary": "#EFB21E", "accent": "#FFFFFF", "names": "forest green, gold, white"},
    "Atlanta Braves": {"primary": "#CE1141", "secondary": "#13274F", "accent": "#FFFFFF", "names": "scarlet red, navy, white"},
    "Baltimore Orioles": {"primary": "#DF4601", "secondary": "#000000", "accent": "#FFFFFF", "names": "orange, black, white"},
    "Boston Red Sox": {"primary": "#BD3039", "secondary": "#0C2340", "accent": "#FFFFFF", "names": "red, navy, white"},
    "Chicago Cubs": {"primary": "#0E3386", "secondary": "#CC3433", "accent": "#FFFFFF", "names": "royal blue, red, white"},
    "Chicago White Sox": {"primary": "#27251F", "secondary": "#C4CED4", "accent": "#FFFFFF", "names": "black, silver, white"},
    "Cincinnati Reds": {"primary": "#C6011F", "secondary": "#000000", "accent": "#FFFFFF", "names": "red, black, white"},
    "Cleveland Guardians": {"primary": "#E31937", "secondary": "#0C2340", "accent": "#FFFFFF", "names": "red, navy, white"},
    "Colorado Rockies": {"primary": "#33006F", "secondary": "#C4CED4", "accent": "#000000", "names": "purple, silver, black"},
    "Detroit Tigers": {"primary": "#0C2340", "secondary": "#FA4616", "accent": "#FFFFFF", "names": "navy, orange, white"},
    "Houston Astros": {"primary": "#EB6E1F", "secondary": "#002D62", "accent": "#FFFFFF", "names": "orange, navy, white"},
    "Kansas City Royals": {"primary": "#004687", "secondary": "#BD9B60", "accent": "#FFFFFF", "names": "royal blue, gold, white"},
    "Los Angeles Angels": {"primary": "#BA0021", "secondary": "#003263", "accent": "#C4CED4", "names": "red, navy, silver"},
    "Los Angeles Dodgers": {"primary": "#005A9C", "secondary": "#FFFFFF", "accent": "#C4CED4", "names": "royal blue, white, silver"},
    "Miami Marlins": {"primary": "#00A3E0", "secondary": "#EF3340", "accent": "#000000", "names": "electric blue, red, black"},
    "Milwaukee Brewers": {"primary": "#12284B", "secondary": "#FFC52F", "accent": "#FFFFFF", "names": "navy, gold, white"},
    "Minnesota Twins": {"primary": "#002B5C", "secondary": "#D31145", "accent": "#FFFFFF", "names": "navy, red, white"},
    "New York Mets": {"primary": "#002D72", "secondary": "#FF5910", "accent": "#FFFFFF", "names": "royal blue, orange, white"},
    "New York Yankees": {"primary": "#0C2340", "secondary": "#FFFFFF", "accent": "#C4CED4", "names": "navy, white, gray"},
    "Philadelphia Phillies": {"primary": "#E81828", "secondary": "#002D72", "accent": "#FFFFFF", "names": "red, navy, white"},
    "Pittsburgh Pirates": {"primary": "#27251F", "secondary": "#FDB827", "accent": "#FFFFFF", "names": "black, gold, white"},
    "San Diego Padres": {"primary": "#2F241D", "secondary": "#FFC425", "accent": "#FFFFFF", "names": "brown, gold, white"},
    "San Francisco Giants": {"primary": "#FD5A1E", "secondary": "#27251F", "accent": "#EFD19F", "names": "orange, black, cream"},
    "Seattle Mariners": {"primary": "#0C2C56", "secondary": "#005C5C", "accent": "#C4CED4", "names": "navy, teal, silver"},
    "St. Louis Cardinals": {"primary": "#C41E3A", "secondary": "#0C2340", "accent": "#FEDB00", "names": "cardinal red, navy, yellow"},
    "Tampa Bay Rays": {"primary": "#092C5C", "secondary": "#8FBCE6", "accent": "#F5D130", "names": "navy, light blue, yellow"},
    "Texas Rangers": {"primary": "#003278", "secondary": "#C0111F", "accent": "#FFFFFF", "names": "royal blue, red, white"},
    "Toronto Blue Jays": {"primary": "#134A8E", "secondary": "#E8291C", "accent": "#FFFFFF", "names": "blue, red, white"},
    "Washington Nationals": {"primary": "#AB0003", "secondary": "#14225A", "accent": "#FFFFFF", "names": "red, navy, white"},
}


TEAM_ALIASES = {
    "D-backs": "Arizona Diamondbacks",
    "Diamondbacks": "Arizona Diamondbacks",
    "A's": "Athletics",
    "Athletics": "Athletics",
    "Braves": "Atlanta Braves",
    "Orioles": "Baltimore Orioles",
    "Red Sox": "Boston Red Sox",
    "Cubs": "Chicago Cubs",
    "White Sox": "Chicago White Sox",
    "Reds": "Cincinnati Reds",
    "Guardians": "Cleveland Guardians",
    "Rockies": "Colorado Rockies",
    "Tigers": "Detroit Tigers",
    "Astros": "Houston Astros",
    "Royals": "Kansas City Royals",
    "Angels": "Los Angeles Angels",
    "Dodgers": "Los Angeles Dodgers",
    "Marlins": "Miami Marlins",
    "Brewers": "Milwaukee Brewers",
    "Twins": "Minnesota Twins",
    "Mets": "New York Mets",
    "Yankees": "New York Yankees",
    "Phillies": "Philadelphia Phillies",
    "Pirates": "Pittsburgh Pirates",
    "Padres": "San Diego Padres",
    "Giants": "San Francisco Giants",
    "Mariners": "Seattle Mariners",
    "Cardinals": "St. Louis Cardinals",
    "Rays": "Tampa Bay Rays",
    "Rangers": "Texas Rangers",
    "Blue Jays": "Toronto Blue Jays",
    "Nationals": "Washington Nationals",
}


DEFAULT_COLORS = {
    "primary": "#FFD630",
    "secondary": "#30AAFF",
    "accent": "#FFFFFF",
    "names": "gold, electric blue, white",
}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    cleaned = value.strip().lstrip("#")
    if len(cleaned) != 6:
        return (255, 255, 255)
    return tuple(int(cleaned[index:index + 2], 16) for index in (0, 2, 4))


def get_team_colors(team_name: str | None) -> dict[str, Any]:
    """Return normalized color data for an MLB team."""
    team = str(team_name or "").strip()
    canonical = team if team in MLB_TEAM_COLORS else TEAM_ALIASES.get(team, team)
    colors = dict(MLB_TEAM_COLORS.get(canonical, DEFAULT_COLORS))
    colors["team"] = canonical if canonical in MLB_TEAM_COLORS else team
    colors["primary_rgb"] = _hex_to_rgb(colors["primary"])
    colors["secondary_rgb"] = _hex_to_rgb(colors["secondary"])
    colors["accent_rgb"] = _hex_to_rgb(colors["accent"])
    return colors
