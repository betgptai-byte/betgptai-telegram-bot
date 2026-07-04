"""Timezone-safe helpers for BETGPTAI schedulers.

Railway/server timezone should never control card timing. Everything here uses
the configured app timezone, defaulting to America/New_York.
"""

from __future__ import annotations

import os
from datetime import datetime
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
