"""BETGPTAI Anime Edition v7.0 MLB mascot prompt styles.

This file is intentionally data-only. Edit the dictionary values when you want
to change how a specific team's anime mascot should look in generated cards.
"""

from __future__ import annotations


BASE_ANIME_CARD_STYLE = (
    "aggressive action pose, glowing eyes, dynamic manga speed lines, dramatic "
    "perspective, premium sports-card illustration, electric lightning effects, "
    "stadium atmosphere, cel-shaded anime rendering, hyper-detailed uniform, "
    "team-color energy effects, high-detail mascot face, championship-level "
    "intensity, Topps Chrome sports card aesthetic, Blue Lock sports anime "
    "energy, premium collectible card quality, mascot occupies 35-45% of the "
    "composition and feels like the hero of the card, high-energy anime sports "
    "trading card, bold manga linework, dramatic stadium lighting, team colors, "
    "clean readable typography, 9:16 vertical poster, premium ESPN x Topps card "
    "x anime style"
)


MLB_MASCOT_STYLE: dict[str, str] = {
    "Baltimore Orioles": f"anime Oriole Bird batter, {BASE_ANIME_CARD_STYLE}",
    "Boston Red Sox": f"anime Wally the Green Monster, {BASE_ANIME_CARD_STYLE}",
    "Chicago White Sox": f"anime Southpaw mascot, {BASE_ANIME_CARD_STYLE}",
    "Cleveland Guardians": f"anime Slider mascot, {BASE_ANIME_CARD_STYLE}",
    "Detroit Tigers": f"anime Paws tiger mascot, {BASE_ANIME_CARD_STYLE}",
    "Houston Astros": f"anime Orbit mascot, {BASE_ANIME_CARD_STYLE}",
    "Kansas City Royals": f"anime Sluggerrr lion mascot, {BASE_ANIME_CARD_STYLE}",
    "Los Angeles Angels": f"anime angel baseball player, {BASE_ANIME_CARD_STYLE}",
    "Minnesota Twins": f"anime T.C. Bear mascot, {BASE_ANIME_CARD_STYLE}",
    "New York Yankees": f"anime pinstripe baseball hero, {BASE_ANIME_CARD_STYLE}",
    "Athletics": f"anime Stomper elephant mascot, {BASE_ANIME_CARD_STYLE}",
    "Oakland Athletics": f"anime Stomper elephant mascot, {BASE_ANIME_CARD_STYLE}",
    "Sacramento Athletics": f"anime Stomper elephant mascot, {BASE_ANIME_CARD_STYLE}",
    "Seattle Mariners": f"anime Mariner Moose, {BASE_ANIME_CARD_STYLE}",
    "Tampa Bay Rays": f"anime Raymond ray mascot, {BASE_ANIME_CARD_STYLE}",
    "Texas Rangers": f"anime Rangers Captain, {BASE_ANIME_CARD_STYLE}",
    "Toronto Blue Jays": f"anime Ace blue jay mascot, {BASE_ANIME_CARD_STYLE}",
    "Arizona Diamondbacks": f"anime D. Baxter bobcat, {BASE_ANIME_CARD_STYLE}",
    "Atlanta Braves": f"anime Blooper mascot, {BASE_ANIME_CARD_STYLE}",
    "Chicago Cubs": f"anime Clark the Cub, {BASE_ANIME_CARD_STYLE}",
    "Cincinnati Reds": f"anime Mr. Redlegs / Gapper style, {BASE_ANIME_CARD_STYLE}",
    "Colorado Rockies": f"anime Dinger dinosaur, {BASE_ANIME_CARD_STYLE}",
    "Los Angeles Dodgers": f"anime Dodger baseball hero, {BASE_ANIME_CARD_STYLE}",
    "Miami Marlins": f"anime Billy the Marlin, {BASE_ANIME_CARD_STYLE}",
    "Milwaukee Brewers": f"anime Bernie Brewer, {BASE_ANIME_CARD_STYLE}",
    "New York Mets": f"anime Mr. Met, {BASE_ANIME_CARD_STYLE}",
    "Philadelphia Phillies": f"anime Phillie Phanatic, {BASE_ANIME_CARD_STYLE}",
    "Pittsburgh Pirates": f"anime Pirate Parrot, {BASE_ANIME_CARD_STYLE}",
    "San Diego Padres": f"anime Swinging Friar, {BASE_ANIME_CARD_STYLE}",
    "San Francisco Giants": f"anime Lou Seal, {BASE_ANIME_CARD_STYLE}",
    "St. Louis Cardinals": f"anime Fredbird, {BASE_ANIME_CARD_STYLE}",
    "Washington Nationals": f"anime Screech eagle, {BASE_ANIME_CARD_STYLE}",
}


MLB_TEAM_ALIASES: dict[str, str] = {
    "Orioles": "Baltimore Orioles",
    "Red Sox": "Boston Red Sox",
    "White Sox": "Chicago White Sox",
    "Guardians": "Cleveland Guardians",
    "Tigers": "Detroit Tigers",
    "Astros": "Houston Astros",
    "Royals": "Kansas City Royals",
    "Angels": "Los Angeles Angels",
    "Twins": "Minnesota Twins",
    "Yankees": "New York Yankees",
    "A's": "Athletics",
    "Athletics": "Athletics",
    "Mariners": "Seattle Mariners",
    "Rays": "Tampa Bay Rays",
    "Rangers": "Texas Rangers",
    "Blue Jays": "Toronto Blue Jays",
    "Diamondbacks": "Arizona Diamondbacks",
    "D-backs": "Arizona Diamondbacks",
    "Braves": "Atlanta Braves",
    "Cubs": "Chicago Cubs",
    "Reds": "Cincinnati Reds",
    "Rockies": "Colorado Rockies",
    "Dodgers": "Los Angeles Dodgers",
    "Marlins": "Miami Marlins",
    "Brewers": "Milwaukee Brewers",
    "Mets": "New York Mets",
    "Phillies": "Philadelphia Phillies",
    "Pirates": "Pittsburgh Pirates",
    "Padres": "San Diego Padres",
    "Giants": "San Francisco Giants",
    "Cardinals": "St. Louis Cardinals",
    "Nationals": "Washington Nationals",
}
