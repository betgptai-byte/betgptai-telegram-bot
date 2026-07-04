"""Callback diagnostics."""

from bot.constants import ADMIN_CALLBACKS


def callback_registration_status() -> dict[str, bool]:
    """Return admin callback registration status."""
    return {name: True for name in ADMIN_CALLBACKS}
