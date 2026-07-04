"""Model weight model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ModelWeight:
    name: str
    value: float
