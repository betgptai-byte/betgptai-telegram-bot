"""Single callback router registration point.

Only this module should register ``CallbackQueryHandler`` instances. The actual
callback execution currently delegates to the existing legacy router so user
behavior remains unchanged during the v3 migration.
"""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler


def register_callback_router(application: Application) -> None:
    """Register the one and only callback router."""
    from main import inline_menu_router

    application.add_handler(CallbackQueryHandler(inline_menu_router))
