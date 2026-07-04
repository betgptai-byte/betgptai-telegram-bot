"""Startup orchestration helpers for BETGPTAI v3.0."""

from __future__ import annotations

from telegram.ext import Application

from bot.callbacks.router import register_callback_router


def register_all(application: Application) -> None:
    """Register v3 routers.

    Command registration is still handled by the legacy runtime during the
    compatibility phase. Callback registration is centralized here.
    """
    register_callback_router(application)


def startup_diagnostics() -> dict[str, str]:
    """Return a small startup diagnostic payload."""
    return {"status": "available", "architecture": "v3 compatibility"}
