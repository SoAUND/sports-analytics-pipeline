"""Clear dollar equivalents for test-only paper units."""

from __future__ import annotations

import os


DEFAULT_PAPER_UNIT_DOLLARS = 5.0


def paper_unit_dollars() -> float:
    """Return the configured hypothetical dollar value of one paper unit."""
    raw = os.environ.get("PAPER_UNIT_DOLLARS", str(DEFAULT_PAPER_UNIT_DOLLARS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("PAPER_UNIT_DOLLARS must be a number") from exc
    if value <= 0 or value > 10_000:
        raise ValueError("PAPER_UNIT_DOLLARS must be greater than 0 and at most 10000")
    return round(value, 2)


def units_to_dollars(units: float | None, *, unit_value: float | None = None) -> float | None:
    """Convert paper profit/loss units to a hypothetical dollar amount."""
    if units is None:
        return None
    value = paper_unit_dollars() if unit_value is None else float(unit_value)
    if value <= 0:
        raise ValueError("unit_value must be greater than 0")
    return round(float(units) * value, 2)
