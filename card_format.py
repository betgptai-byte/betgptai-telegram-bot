"""Shared Telegram card text used across public and premium commands."""

from card_time import CARD_TIMING_FOOTER
from game_time import GAME_TIME_FOOTER

DIVIDER = "━━━━━━━━━━━━"

PARLAY_NOTE = """⚠️ BETGPTAI NOTE

These legs are individually selected as strong standalone plays.

Single bets are recommended for the best long-term results.

Parlays increase variance and should be played at your own risk.

Educational analysis only."""

# Keeping this footer in one place makes every card use the exact same
# responsible-play language now and when more premium commands are added.
RECOMMENDATION_FOOTER = f"""{DIVIDER}

⚠️ BETGPTAI RECOMMENDATION

✅ These plays are designed to be played as SINGLE BETS for the best long-term results.

🧩 Parlays are for entertainment and higher risk.

If choosing to parlay, do so at your own risk and consider reducing stake size.

Past performance does not guarantee future results.

Educational analysis only. Play responsibly.

{DIVIDER}"""

ODDS_SHOPPING_FOOTER = """📌 Odds vary by sportsbook.
Please shop for the best available number before playing.

Singles are recommended for better long-term results.
Parlays are optional and higher risk."""

TIMED_CARD_FOOTER = (
    f"{RECOMMENDATION_FOOTER}\n\n{ODDS_SHOPPING_FOOTER}\n\n"
    f"{CARD_TIMING_FOOTER}\n\n{GAME_TIME_FOOTER}"
)
