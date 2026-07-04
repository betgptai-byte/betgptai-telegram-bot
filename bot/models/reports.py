"""Report model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Report:
    name: str
    card_date: str
    payload: dict[str, Any]
