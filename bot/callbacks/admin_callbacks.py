"""Admin callback dispatch lives here in the full v3 architecture."""

from __future__ import annotations

from bot.constants import ADMIN_CALLBACKS


def registered_admin_callbacks() -> tuple[str, ...]:
    """Return known admin callback names."""
    return ADMIN_CALLBACKS
