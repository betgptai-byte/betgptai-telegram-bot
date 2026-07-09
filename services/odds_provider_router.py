"""Odds Provider Router — Sharp API primary, The Odds API as backup.

Environment variables:
  SHARP_API_ENABLED=true           Enable Sharp API (default: true)
  SHARP_API_KEY                    Sharp API key (required if enabled)
  SHARP_API_BASE_URL               Sharp API base URL
  ODDS_API_ENABLED=true            Enable The Odds API (default: true)
  ODDS_API_BACKUP_ONLY=true        Odds API is fallback only (default: true)

Flow:
  1. Sharp enabled → fetch from Sharp API (cached, rate-limited)
  2. Sharp fails OR Sharp disabled AND Odds API not backup-only → fetch Odds API
  3. Both fail → return empty list

Each returned event has best_available_prices merged from all bookmakers.
Slate enrichment attaches a standard market_context dict to every game.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from api.sharp_odds_client import (
    SharpAPIError,
    SharpRateLimitError,
    build_game_market_context,
    fetch_mlb_odds as sharp_fetch,
    odds_api_backup_only,
    odds_api_enabled,
    sharp_api_enabled,
    sharp_api_key as sharp_key,
)
from mlb_data import MLBDataError, get_mlb_odds as odds_api_fetch

logger = logging.getLogger(__name__)


def fetch_odds() -> list[dict[str, Any]]:
    """Fetch MLB odds — Sharp primary, Odds API fallback.

    Returns normalized events in The Odds API shape.
    Returns empty list if all providers fail.
    """
    sharp_ok = sharp_api_enabled() and bool(sharp_key())
    odds_ok = odds_api_enabled() and bool(os.getenv("ODDS_API_KEY", "").strip())

    sharp_error: str | None = None

    if sharp_ok:
        try:
            odds = sharp_fetch()
            if odds:
                logger.info("Odds provider router: using Sharp API (%d events)", len(odds))
                return odds
            sharp_error = "Sharp returned empty response"
        except SharpRateLimitError:
            sharp_error = "Sharp rate-limited (429)"
            logger.warning("Odds provider router: %s", sharp_error)
        except SharpAPIError as exc:
            sharp_error = str(exc)
            logger.warning("Odds provider router: Sharp failed — %s", sharp_error)
        except Exception as exc:
            sharp_error = f"Sharp unexpected error: {exc}"
            logger.warning("Odds provider router: %s", sharp_error)

    backup_only = odds_api_backup_only()
    if odds_ok and (not backup_only or sharp_error is not None):
        odds_key = os.getenv("ODDS_API_KEY", "")
        try:
            odds = odds_api_fetch(odds_key)
            if odds:
                logger.info(
                    "Odds provider router: using The Odds API (Sharp: %s)",
                    sharp_error or "disabled",
                )
                return odds
        except MLBDataError as exc:
            logger.warning("Odds provider router: Odds API failed — %s", exc)
        except Exception as exc:
            logger.warning("Odds provider router: Odds API unexpected error — %s", exc)

    msg = "All odds providers unavailable"
    if sharp_error:
        msg += f" (Sharp: {sharp_error})"
    logger.error(msg)
    return []


def enrich_slate_market_context(
    slate: list[dict[str, Any]],
    odds: list[dict[str, Any]],
    provider: str,
) -> list[dict[str, Any]]:
    """Attach a standard market_context dict to every game in the slate.

    market_context includes:
      provider, odds_found, moneyline, runline, total, team_totals,
      last_updated, matched_game_pk, matched_by
    """
    for game in slate:
        prices = game.get("best_available_prices")
        if not isinstance(prices, list):
            prices = []
        game["market_context"] = build_game_market_context(
            game, prices, provider,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
    return slate


def provider_status() -> dict[str, Any]:
    """Return status of both providers for diagnostics."""
    return {
        "sharp_api": {
            "enabled": sharp_api_enabled(),
            "key_loaded": bool(sharp_key()),
        },
        "odds_api": {
            "enabled": odds_api_enabled(),
            "backup_only": odds_api_backup_only(),
            "key_loaded": bool(os.getenv("ODDS_API_KEY", "").strip()),
        },
    }
