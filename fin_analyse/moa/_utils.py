"""Shared internal utilities for the MoA module."""

from __future__ import annotations


def list_str(value: object) -> list[str]:
    """Return a list of strings from a value, or an empty list."""
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def clamp_float(value: object, low: float, high: float) -> float:
    """Clamp a value to [low, high], coercing non-numeric input to low."""
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            number = low
    else:
        number = low
    return min(max(number, low), high)
