"""Human-readable letter grades for the 0-100 Pick Strength Score.

The grade is a display label only. It is not a win probability and cannot
override a failed research, price, edge, expected-value, or integrity gate.
"""

from __future__ import annotations


GRADE_SCALE: tuple[tuple[int, str], ...] = (
    (95, "A+"),
    (90, "A"),
    (85, "A-"),
    (82, "B+"),
    (79, "B"),
    (75, "B-"),
    (70, "C+"),
    (65, "C"),
    (60, "C-"),
    (55, "D+"),
    (50, "D"),
    (45, "D-"),
    (35, "F+"),
    (20, "F"),
    (0, "F-"),
)


def pick_strength_grade(score: int | None) -> str | None:
    """Convert a validated Pick Strength Score to its display grade."""

    if score is None:
        return None
    if isinstance(score, bool) or not isinstance(score, int):
        raise TypeError("Pick Strength Score must be an integer or None")
    if not 0 <= score <= 100:
        raise ValueError("Pick Strength Score must be between 0 and 100")
    return next(grade for minimum, grade in GRADE_SCALE if score >= minimum)


def format_pick_strength(score: int | None) -> str | None:
    """Return a clear score-plus-grade label for user-facing result payloads."""

    grade = pick_strength_grade(score)
    return None if grade is None else f"{score}/100 (Grade {grade})"
