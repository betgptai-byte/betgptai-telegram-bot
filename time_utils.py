"""Timezone-safe helpers for BETGPTAI schedulers.

Railway/server timezone should never control card timing. Everything here uses
the configured app timezone, defaulting to America/New_York.
"""

from __future__ import annotations

import os
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "America/New_York"


def get_app_timezone() -> ZoneInfo:
    """Return the official BETGPTAI timezone."""
    name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def now_et() -> datetime:
    """Return a timezone-aware current datetime in the official app timezone."""
    return datetime.now(get_app_timezone())


def to_et(dt: Any) -> datetime | None:
    """Convert a datetime or ISO timestamp into the official app timezone."""
    if isinstance(dt, str):
        try:
            parsed = datetime.fromisoformat(dt.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(dt, datetime):
        parsed = dt
    else:
        return None
    timezone = get_app_timezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def format_et(dt: Any) -> str:
    """Format a datetime as a short Eastern/app-time display."""
    parsed = to_et(dt)
    if parsed is None:
        return "Unavailable"
    return f"{parsed.strftime('%I:%M %p').lstrip('0')} ET"


def mlb_local_game_date(dt_utc: Any, timezone_name: str = DEFAULT_TIMEZONE) -> str | None:
    """Return the MLB calendar date after converting a UTC timestamp locally."""
    if isinstance(dt_utc, str):
        try:
            parsed = datetime.fromisoformat(dt_utc.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(dt_utc, datetime):
        parsed = dt_utc
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        local_tz = ZoneInfo(timezone_name)
    except Exception:
        local_tz = ZoneInfo(DEFAULT_TIMEZONE)
    return parsed.astimezone(local_tz).date().isoformat()


def mlb_utc_query_window(card_date: str, timezone_name: str = DEFAULT_TIMEZONE) -> tuple[str, str]:
    """Convert one Eastern MLB card day into its exact UTC query bounds."""
    selected = date.fromisoformat(card_date)
    try:
        local_tz = ZoneInfo(timezone_name)
    except Exception:
        local_tz = ZoneInfo(DEFAULT_TIMEZONE)
    start = datetime.combine(selected, time.min, tzinfo=local_tz).astimezone(timezone.utc)
    end = datetime.combine(selected, time.max, tzinfo=local_tz).astimezone(timezone.utc)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")
