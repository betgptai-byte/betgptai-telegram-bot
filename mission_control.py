"""Owner-only Mission Control helpers.

This module keeps small Mission Control status snippets out of the public bot
surface. It should never be imported for member-facing copy.
"""

from __future__ import annotations

from model_weights import ai_learning_auto_apply_enabled
from api.sharp_client import health as sharp_health


def ai_learning_auto_apply_line() -> str:
    """Return the Mission Control line for AI Learning auto-apply."""
    return f"AI Learning Auto Apply: {'ON' if ai_learning_auto_apply_enabled() else 'OFF'}"


def sharp_api_status_line() -> str:
    """Return the Mission Control line for Sharp API health."""
    try:
        h = sharp_health()
        if not h.get("enabled"):
            return "Sharp API: Disabled"
        if not h.get("api_key_loaded"):
            return "Sharp API: No key configured"
        fresh = h.get("cache_fresh", False)
        age = h.get("cache_age_seconds")
        age_str = f"{age:.0f}s" if age is not None else "N/A"
        return f"Sharp API: {'Cached' if fresh else 'Stale'} ({age_str})"
    except Exception:
        return "Sharp API: Unknown"
