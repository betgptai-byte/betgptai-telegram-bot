"""Sharp API — primary odds provider with MarketContext support.

Extends api.sharp_client with the market_context format required by
the Odds Provider Router and market_context validation.

market_context dict format:
  provider       — "sharp_api" or "odds_api"
  odds_found     — bool
  moneyline      — list[dict]  (label, odds)
  runline        — list[dict]  (label, line, odds)
  total           — list[dict]  (label, line, odds)
  team_totals     — list[dict]  (label, line, odds)
  last_updated   — str | None
  matched_game_pk — int | None
  matched_by     — str
"""
from __future__ import annotations

from typing import Any

from api.sharp_client import (
    SharpAPIError,
    SharpRateLimitError,
    build_market_context as _build_market_context_obj,
    clear_cache,
    fetch_mlb_odds,
    health,
    odds_api_backup_only,
    odds_api_enabled,
    sharp_api_enabled,
    sharp_api_key,
)
from core.market import MarketContext

__all__ = [
    "SharpAPIError",
    "SharpRateLimitError",
    "MarketContext",
    "build_game_market_context",
    "clear_cache",
    "fetch_mlb_odds",
    "health",
    "odds_api_backup_only",
    "odds_api_enabled",
    "sharp_api_enabled",
    "sharp_api_key",
]


def build_game_market_context(
    game: dict[str, Any],
    prices: list[dict[str, Any]],
    provider: str,
    last_updated: str | None = None,
) -> dict[str, Any]:
    """Build a market_context dict in the standard format.

    This dict is attached to each game row in the slate.  It is the
    single source of truth for market-context validation in
    ``_filter_public_market_context``.
    """
    ctx = _market_context_from_prices(prices)
    matched_game_pk = game.get("game_pk") or game.get("game_id")
    try:
        matched_game_pk = int(matched_game_pk) if matched_game_pk is not None else None
    except (TypeError, ValueError):
        matched_game_pk = None
    return {
        "provider": provider,
        "odds_found": bool(prices),
        "moneyline": ctx.get("moneyline", []),
        "runline": ctx.get("runline", []),
        "total": ctx.get("total", []),
        "team_totals": ctx.get("team_totals", []),
        "last_updated": last_updated,
        "matched_game_pk": matched_game_pk,
        "matched_by": provider,
    }


def _market_context_from_prices(prices: list[dict[str, Any]]) -> dict[str, Any]:
    """Convert best_available_prices to the standard market_context format."""
    context: dict[str, Any] = {
        "moneyline": [], "runline": [], "total": [], "team_totals": [],
    }
    for price in prices:
        market = price.get("market")
        label = price.get("description") or price.get("outcome")
        point = price.get("point")
        american = price.get("price")
        entry = {"label": label, "odds": american}
        if market == "h2h":
            context["moneyline"].append(entry)
        elif market == "spreads":
            entry_with_line = {**entry, "line": point}
            context["runline"].append(entry_with_line)
        elif market == "totals":
            entry_with_line = {**entry, "line": point}
            context["total"].append(entry_with_line)
        elif market == "team_totals":
            entry_with_line = {**entry, "line": point}
            context["team_totals"].append(entry_with_line)
    return context
