"""Validation helpers."""

from __future__ import annotations


def is_non_empty(value: object) -> bool:
    """Return True when value is meaningfully present."""
    return bool(str(value or "").strip())
