"""Owner-only Mission Control helpers.

This module keeps small Mission Control status snippets out of the public bot
surface. It should never be imported for member-facing copy.
"""

from __future__ import annotations

from model_weights import ai_learning_auto_apply_enabled


def ai_learning_auto_apply_line() -> str:
    """Return the Mission Control line for AI Learning auto-apply."""
    return f"AI Learning Auto Apply: {'ON' if ai_learning_auto_apply_enabled() else 'OFF'}"
