"""Odds Provider Router — Sharp API primary for all sports, The Odds API backup.

Environment variables:
  SHARP_API_ENABLED=true           Enable Sharp API (default: true)
  SHARP_API_KEY                    Sharp API key (required if enabled)
  SHARP_API_BASE_URL               Sharp API base URL
  ODDS_API_ENABLED=true            Enable The Odds API (default: true)
  ODDS_API_BACKUP_ONLY=true        Odds API is fallback only (default: true)

Supported sports:
  mlb, soccer, nba, nfl, nhl

Flow:
  1. Sharp enabled → fetch from Sharp API (cached, rate-limited)
  2. Sharp fails → fall back to The Odds API (MLB only — Odds API only supports MLB)
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
    SHARP_SPORT_MAP,
    SharpAPIError,
    SharpRateLimitError,
    build_game_market_context,
    get_odds as sharp_get_odds,
    odds_api_backup_only,
    odds_api_enabled,
    sharp_api_enabled,
    sharp_api_key as sharp_key,
)
from mlb_data import MLBDataError, get_mlb_odds as odds_api_fetch

logger = logging.getLogger(__name__)


def fetch_odds(
    sport: str = "mlb",
    league: str | None = None,
    event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch odds for *sport* — Sharp primary, Odds API fallback (MLB only).

    Args:
        sport: One of ``mlb``, ``soccer``, ``nba``, ``nfl``, ``nhl``.
        league: Optional league override (e.g. ``"EPL"`` for soccer).
        event_date: Optional ISO date to scope the request.

    Returns normalized events in The Odds API shape.
    Returns empty list if all providers fail.
    """
    sport_lower = sport.lower()
    if sport_lower not in SHARP_SPORT_MAP:
        logger.warning("Unsupported sport for odds fetch: %s", sport)
        return []

    sharp_ok = sharp_api_enabled() and bool(sharp_key())
    odds_ok = odds_api_enabled() and bool(os.getenv("ODDS_API_KEY", "").strip())

    sharp_error: str | None = None

    # ── 1. Try Sharp API (primary for all sports) ──────────────────────
    if sharp_ok:
        try:
            cfg = SHARP_SPORT_MAP[sport_lower]
            league_param = league or cfg.get("league")
            odds = sharp_get_odds(sport=sport_lower, league=league_param, event_date=event_date)
            if odds:
                logger.info(
                    "Odds provider router: Sharp API for %s (%d events, league=%s)",
                    sport_lower, len(odds), league_param or "all",
                )
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

    # ── 2. Fall back to The Odds API (MLB only) ────────────────────────
    # Try Odds API when:
    #   - backup_only=False (always try Odds API)
    #   - Sharp failed (any error, including empty response)
    #   - Sharp is disabled entirely (sharp_ok=False means no Sharp at all)
    backup_only = odds_api_backup_only()
    if sport_lower == "mlb" and odds_ok and (not backup_only or sharp_error is not None or not sharp_ok):
        odds_key = os.getenv("ODDS_API_KEY", "")
        try:
            odds = odds_api_fetch(odds_key)
            if odds:
                logger.info(
                    "Odds provider router: The Odds API for %s (Sharp: %s)",
                    sport_lower, sharp_error or "disabled",
                )
                return odds
        except MLBDataError as exc:
            logger.warning("Odds provider router: Odds API failed — %s", exc)
        except Exception as exc:
            logger.warning("Odds provider router: Odds API unexpected error — %s", exc)

    msg = f"All odds providers unavailable for {sport_lower}"
    if sharp_error:
        msg += f" (Sharp: {sharp_error})"
    logger.error(msg)
    return []


def enrich_slate_market_context(
    slate: list[dict[str, Any]],
    odds: list[dict[str, Any]],
    provider: str,
    *,
    sport: str = "mlb",
    league: str | None = None,
) -> list[dict[str, Any]]:
    """Attach a standard market_context dict to every game in the slate."""
    for game in slate:
        prices = game.get("best_available_prices")
        if not isinstance(prices, list):
            prices = []
        game["market_context"] = build_game_market_context(
            game, prices, provider,
            sport=sport, league=league,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )
    return slate


def provider_status(sport: str = "mlb") -> dict[str, Any]:
    """Return status of both providers for one sport."""
    from api.sharp_odds_client import health as sharp_health

    return {
        "sharp_api": {
            "enabled": sharp_api_enabled(),
            "key_loaded": bool(sharp_key()),
            "health": sharp_health(sport=sport),
        },
        "odds_api": {
            "enabled": odds_api_enabled(),
            "backup_only": odds_api_backup_only(),
            "key_loaded": bool(os.getenv("ODDS_API_KEY", "").strip()),
            "supported_for_sport": sport == "mlb",
        },
    }
