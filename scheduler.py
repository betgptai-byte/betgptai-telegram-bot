"""Compatibility wrapper for BETGPTAI scheduling.

The active Railway loop lives in posting_scheduler.py. This module exists so
future imports can use the shorter scheduler name without duplicating logic.
"""

from posting_scheduler import (  # noqa: F401
    process_game_aware_posts,
    run_game_aware_scheduler,
    scheduler_status_text,
    time_debug_text,
)
