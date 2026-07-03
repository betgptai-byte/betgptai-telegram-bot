"""Official BETGPTAI sports-day timing in America/New_York."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
CARD_UNLOCK_TIME = time(3, 0)
CARD_TIMING_FOOTER = (
    "Card timing follows Eastern Time. "
    "Next-day cards unlock after 3:00 AM ET."
)


def eastern_now() -> datetime:
    """Return the current timezone-aware time in the bot's official timezone."""
    return datetime.now(EASTERN)


def official_sports_date(now: datetime | None = None) -> date:
    """Return the active card date using the 3:00 AM Eastern rollover."""
    current = now or eastern_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=EASTERN)
    else:
        current = current.astimezone(EASTERN)
    sports_date = current.date()
    if current.time().replace(tzinfo=None) < CARD_UNLOCK_TIME:
        sports_date -= timedelta(days=1)
    return sports_date


def tomorrow_sports_date(now: datetime | None = None) -> date:
    """Return the sports day immediately after the currently active card."""
    return official_sports_date(now) + timedelta(days=1)
