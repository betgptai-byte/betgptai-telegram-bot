"""Result summary model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ResultSummary:
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    pending: int = 0
    profit_units: float = 0.0
