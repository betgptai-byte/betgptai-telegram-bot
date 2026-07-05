"""Market value scoring and price rules for BETGPTAI v20."""

from __future__ import annotations

from typing import Any

from edge_database import clamp


def implied_probability(american: Any) -> float | None:
    """Convert American odds to implied probability."""
    if not isinstance(american, (int, float)) or american == 0:
        return None
    return abs(american) / (abs(american) + 100) if american < 0 else 100 / (american + 100)


def market_value_score(game: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Score available market quality and identify price-rule passes."""
    prices = game.get("best_available_prices") if isinstance(game.get("best_available_prices"), list) else []
    moneylines = [row for row in prices if isinstance(row, dict) and row.get("market") == "h2h"]
    available = len(prices)
    score = 50 + min(available, 8) * 4
    pass_reasons: list[str] = []
    for row in moneylines:
        price = row.get("price")
        if isinstance(price, (int, float)) and price <= -190:
            pass_reasons.append(f"{row.get('outcome')} ML {price:g} price too expensive")
    return clamp(score), {"available_fields": available, "possible_fields": 8, "price_pass_reasons": pass_reasons}
