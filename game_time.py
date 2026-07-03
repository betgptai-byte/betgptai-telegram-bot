"""Shared Eastern Time formatting for MLB and soccer Telegram cards."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from card_time import EASTERN, eastern_now


GAME_TIME_FOOTER = "⏰ All game times are listed in Eastern Time (ET)."


def parse_game_time(value: Any) -> datetime | None:
    """Parse the ISO timestamps returned by the sports APIs."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def game_sort_key(game: dict[str, Any], time_key: str = "game_time") -> datetime:
    """Return a stable Eastern timestamp for chronological card sorting."""
    parsed = parse_game_time(game.get(time_key))
    return parsed or datetime.max.replace(tzinfo=EASTERN)


def format_game_clock(
    value: Any,
    *,
    status: Any = None,
    now: datetime | None = None,
) -> str:
    """Return a 12-hour ET time, or a neutral started label after kickoff."""
    parsed = parse_game_time(value)
    current = (now or eastern_now()).astimezone(EASTERN)
    status_text = str(status or "").lower()
    started_status = any(
        word in status_text
        for word in ("live", "in progress", "in_play", "paused", "final", "game over")
    )
    if started_status or (parsed is not None and parsed <= current):
        return "🕒 Started"
    if parsed is None:
        return "🕒 Time unavailable ET"
    return f"🕒 {parsed.strftime('%I:%M %p').lstrip('0')} ET"


def mlb_game_block(game: dict[str, Any], now: datetime | None = None) -> str:
    """Format one MLB matchup and its official Eastern start time."""
    return (
        f"🆚 {game.get('away_team', 'Away Team')} @ "
        f"{game.get('home_team', 'Home Team')}\n"
        f"{format_game_clock(game.get('game_time'), status=game.get('status'), now=now)}"
    )


def soccer_game_block(game: dict[str, Any], now: datetime | None = None) -> str:
    """Format one soccer matchup and its official Eastern kickoff time."""
    return (
        f"🆚 {game.get('home_team', 'Home Team')} vs "
        f"{game.get('away_team', 'Away Team')}\n"
        f"{format_game_clock(game.get('kickoff'), status=game.get('status'), now=now)}"
    )
