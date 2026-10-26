"""Auditable betting calculations for NFL point spreads."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, getcontext
from typing import Iterable


getcontext().prec = 28
ZERO = Decimal("0")
ONE = Decimal("1")
DEFAULT_EDGE = Decimal("0.05")
PROBABILITY_TOLERANCE = Decimal("0.000000001")


class CalculationError(ValueError):
    """Raised when odds or probabilities cannot support a safe calculation."""


def as_decimal(value: Decimal | int | float | str) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CalculationError(f"Invalid number: {value!r}") from exc


def _probability(value: Decimal | int | float | str, name: str) -> Decimal:
    result = as_decimal(value)
    if result < ZERO or result > ONE:
        raise CalculationError(f"{name} must be between 0 and 1")
    return result


def american_to_decimal(american_odds: Decimal | int | str) -> Decimal:
    odds = as_decimal(american_odds)
    if odds == ZERO:
        raise CalculationError("American odds cannot be zero")
    if odds > ZERO:
        return ONE + (odds / Decimal("100"))
    return ONE + (Decimal("100") / abs(odds))


def decimal_to_american(decimal_odds: Decimal | int | float | str) -> Decimal:
    odds = as_decimal(decimal_odds)
    if odds <= ONE:
        raise CalculationError("Decimal odds must be greater than 1")
    if odds >= Decimal("2"):
        return (odds - ONE) * Decimal("100")
    return -Decimal("100") / (odds - ONE)


def break_even_probability(decimal_odds: Decimal | int | float | str) -> Decimal:
    odds = as_decimal(decimal_odds)
    if odds <= ONE:
        raise CalculationError("Decimal odds must be greater than 1")
    return ONE / odds


def remove_vig(decimal_prices: Iterable[Decimal | int | float | str]) -> tuple[Decimal, ...]:
    prices = tuple(as_decimal(price) for price in decimal_prices)
    if len(prices) < 2:
        raise CalculationError("No-vig calculation requires at least two outcomes")
    raw = tuple(break_even_probability(price) for price in prices)
    market_total = sum(raw, ZERO)
    if market_total <= ZERO:
        raise CalculationError("Market probability total must be positive")
    quantum = Decimal("0.000000000000000001")
    normalized = [
        (probability / market_total).quantize(quantum)
        for probability in raw[:-1]
    ]
    normalized.append(ONE - sum(normalized, ZERO))
    return tuple(normalized)


def standard_ev(
    win_probability: Decimal | int | float | str,
    decimal_odds: Decimal | int | float | str,
) -> Decimal:
    probability = _probability(win_probability, "win_probability")
    odds = as_decimal(decimal_odds)
    if odds <= ONE:
        raise CalculationError("Decimal odds must be greater than 1")
    return (probability * odds) - ONE


def push_aware_ev(
    win_probability: Decimal | int | float | str,
    loss_probability: Decimal | int | float | str,
    push_probability: Decimal | int | float | str,
    decimal_odds: Decimal | int | float | str,
) -> Decimal:
    p_win = _probability(win_probability, "win_probability")
    p_loss = _probability(loss_probability, "loss_probability")
    p_push = _probability(push_probability, "push_probability")
    total = p_win + p_loss + p_push
    if abs(total - ONE) > PROBABILITY_TOLERANCE:
        raise CalculationError("Win, loss, and push probabilities must sum to 1")
    odds = as_decimal(decimal_odds)
    if odds <= ONE:
        raise CalculationError("Decimal odds must be greater than 1")
    return (p_win * (odds - ONE)) - p_loss


def conservative_push_probabilities(
    conditional_cover_probability: Decimal | int | float | str,
    push_probability: Decimal | int | float | str,
    uncertainty_margin: Decimal | int | float | str,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    conditional = _probability(
        conditional_cover_probability, "conditional_cover_probability"
    )
    p_push = _probability(push_probability, "push_probability")
    uncertainty = _probability(uncertainty_margin, "uncertainty_margin")
    conservative_conditional = conditional - uncertainty
    if conservative_conditional < ZERO:
        raise CalculationError("Uncertainty cannot reduce probability below zero")
    non_push = ONE - p_push
    p_win = non_push * conservative_conditional
    p_loss = non_push * (ONE - conservative_conditional)
    return p_win, p_loss, p_push, conservative_conditional


def probability_edge(
    conservative_probability: Decimal | int | float | str,
    decimal_odds: Decimal | int | float | str,
) -> Decimal:
    probability = _probability(conservative_probability, "conservative_probability")
    return probability - break_even_probability(decimal_odds)


def fair_decimal_odds(probability: Decimal | int | float | str) -> Decimal:
    p = _probability(probability, "probability")
    if p == ZERO:
        raise CalculationError("Probability must be greater than zero")
    return ONE / p


def minimum_acceptable_decimal_odds(
    conservative_probability: Decimal | int | float | str,
    required_edge: Decimal | int | float | str = DEFAULT_EDGE,
) -> Decimal:
    probability = _probability(conservative_probability, "conservative_probability")
    edge = _probability(required_edge, "required_edge")
    maximum_break_even = probability - edge
    if maximum_break_even <= ZERO:
        raise CalculationError("Probability is too low to preserve the required edge")
    return ONE / maximum_break_even


def qualifies_price(
    offered_decimal_odds: Decimal | int | float | str,
    conservative_probability: Decimal | int | float | str,
    required_edge: Decimal | int | float | str = DEFAULT_EDGE,
) -> bool:
    offered = as_decimal(offered_decimal_odds)
    if offered <= ONE:
        raise CalculationError("Decimal odds must be greater than 1")
    return offered >= minimum_acceptable_decimal_odds(
        conservative_probability, required_edge
    )
