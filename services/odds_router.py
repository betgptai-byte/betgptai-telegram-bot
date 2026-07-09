"""Odds Router — primary Sharp API with The Odds API as fallback.

Environment variables:
  SHARP_API_ENABLED=true          Enable Sharp API (default: true)
  SHARP_API_KEY                   Sharp API key (required if enabled)
  SHARP_API_BASE_URL              Sharp API base URL
  ODDS_API_ENABLED=true           Enable The Odds API (default: true)
  ODDS_API_BACKUP_ONLY=true       Odds API is fallback only (default: true)

Flow:
  1. If Sharp enabled → fetch from Sharp API (cached, rate-limited)
  2. If Sharp fails OR Sharp disabled AND Odds API not backup-only → fetch Odds API
  3. If both disabled/fail → return empty list
"""
from __future__ import annotations

import logging
import os
from typing import Any

from api.sharp_client import (
    SharpAPIError,
    SharpRateLimitError,
    fetch_mlb_odds as sharp_fetch,
    sharp_api_enabled,
    sharp_api_key as sharp_key,
)
from mlb_data import get_mlb_odds as odds_api_fetch, MLBDataError

logger = logging.getLogger(__name__)


def _odds_api_backup_only() -> bool:
    return os.getenv("ODDS_API_BACKUP_ONLY", "true").strip().lower() in ("1", "true", "yes", "on")


def _odds_api_enabled() -> bool:
    return os.getenv("ODDS_API_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def fetch_odds() -> list[dict[str, Any]]:
    """Fetch MLB odds — Sharp primary, Odds API fallback.

    Returns normalized events in The Odds API shape.
    Raises RuntimeError if both providers fail.
    """
    sharp_ok = sharp_api_enabled() and bool(sharp_key())
    odds_ok = _odds_api_enabled() and bool(os.getenv("ODDS_API_KEY", "").strip())

    sharp_error: str | None = None

    # ── 1. Try Sharp API (primary) ─────────────────────────────────────
    if sharp_ok:
        try:
            odds = sharp_fetch()
            if odds:
                logger.info("Odds router: using Sharp API (%d events)", len(odds))
                return odds
            sharp_error = "Sharp returned empty response"
        except SharpRateLimitError:
            sharp_error = "Sharp rate-limited (429)"
            logger.warning("Odds router: %s", sharp_error)
        except SharpAPIError as exc:
            sharp_error = str(exc)
            logger.warning("Odds router: Sharp failed — %s", sharp_error)
        except Exception as exc:
            sharp_error = f"Sharp unexpected error: {exc}"
            logger.warning("Odds router: %s", sharp_error)

    # ── 2. Fall back to The Odds API ───────────────────────────────────
    backup_only = _odds_api_backup_only()
    if odds_ok and (not backup_only or sharp_error is not None):
        odds_key = os.getenv("ODDS_API_KEY", "")
        try:
            odds = odds_api_fetch(odds_key)
            if odds:
                logger.info(
                    "Odds router: using The Odds API (Sharp: %s)",
                    sharp_error or "disabled",
                )
                return odds
        except MLBDataError as exc:
            logger.warning("Odds router: Odds API failed — %s", exc)
        except Exception as exc:
            logger.warning("Odds router: Odds API unexpected error — %s", exc)

    # ── 3. Both failed ─────────────────────────────────────────────────
    msg = "All odds providers unavailable"
    if sharp_error:
        msg += f" (Sharp: {sharp_error})"
    logger.error(msg)
    return []


def provider_status() -> dict[str, Any]:
    """Return status of both providers for diagnostics."""
    return {
        "sharp_api": {
            "enabled": sharp_api_enabled(),
            "key_loaded": bool(sharp_key()),
        },
        "odds_api": {
            "enabled": _odds_api_enabled(),
            "backup_only": _odds_api_backup_only(),
            "key_loaded": bool(os.getenv("ODDS_API_KEY", "").strip()),
        },
    }
