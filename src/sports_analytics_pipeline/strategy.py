"""Test-only Best Market Expression strategy layer.

This module does not create new probabilities. It consumes already researched,
timestamped candidate outputs and ranks the available expressions using the
approved strategy: conservative probability, price, expected value, stability,
protection, and correlation discipline. The strategy is sport-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import prod
from typing import Any, Iterable, Mapping, Sequence

from .market_expression import scan_market_expressions

D = Decimal
STRATEGY_VERSION = "best_market_expression_v2"


@dataclass(frozen=True)
class MarketExpression:
    key: str
    market: str
    decimal_odds: Decimal
    conservative_probability: Decimal
    break_even_probability: Decimal
    ev: Decimal
    probability_edge: Decimal
    uncertainty_margin: Decimal = D("0")
    information_quality: str = "unknown"
    stability_status: str = "unknown"
    protection_score: Decimal = D("0")
    threshold: Decimal | None = None
    source: Mapping[str, Any] | None = None

    @property
    def qualifies(self) -> bool:
        return (
            self.ev > 0
            and self.probability_edge >= D("0.05")
            and self.stability_status not in {"unstable", "failed"}
            and self.information_quality not in {"missing_or_conflicting", "conflicting"}
        )


def break_even_probability(decimal_odds: Decimal) -> Decimal:
    if decimal_odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return D("1") / decimal_odds


def expected_value(probability: Decimal, decimal_odds: Decimal) -> Decimal:
    return probability * decimal_odds - D("1")


def protection_score(
    *,
    probability: Decimal,
    adjacent_probability: Decimal | None = None,
    uncertainty_margin: Decimal = D("0"),
) -> Decimal:
    base = max(D("0"), min(D("1"), probability - uncertainty_margin))
    bonus = D("0")
    if adjacent_probability is not None:
        bonus = max(D("0"), min(D("0.10"), probability - adjacent_probability))
    return max(D("0"), min(D("1"), base + bonus))


def threshold_efficiency(
    *,
    probability: Decimal,
    adjacent_probability: Decimal,
    decimal_odds: Decimal,
    adjacent_decimal_odds: Decimal,
) -> Decimal:
    prob_gain = probability - adjacent_probability
    price_cost = break_even_probability(decimal_odds) - break_even_probability(adjacent_decimal_odds)
    if price_cost == 0:
        return D("0")
    return prob_gain / abs(price_cost)


def rank_score(expr: MarketExpression) -> Decimal:
    if not expr.qualifies:
        return D("-999")
    info = {
        "complete": D("1"),
        "complete_current": D("1"),
        "full_official": D("1"),
        "high": D("0.9"),
        "complete_minor_uncertainty": D("0.8"),
        "projected": D("0.6"),
        "unknown": D("0.4"),
    }.get(expr.information_quality, D("0.5"))
    stability = D("1") if expr.stability_status in {"stable", "passed", "robust"} else D("0.65")
    return (
        expr.ev * D("45")
        + expr.probability_edge * D("30")
        + expr.protection_score * D("10")
        + info * D("8")
        + stability * D("7")
        - expr.uncertainty_margin * D("20")
    )


def choose_best_market_expression(expressions: Iterable[MarketExpression]) -> MarketExpression | None:
    qualified = [item for item in expressions if item.qualifies]
    if not qualified:
        return None
    return max(qualified, key=lambda item: (rank_score(item), item.ev, item.probability_edge))


def independent_joint_probability(probabilities: Sequence[Decimal]) -> Decimal:
    if not probabilities:
        raise ValueError("at least one probability is required")
    if any(p < 0 or p > 1 for p in probabilities):
        raise ValueError("probabilities must be between 0 and 1")
    return D(str(prod(float(p) for p in probabilities)))


def evaluate_parlay(
    *,
    probabilities: Sequence[Decimal],
    parlay_decimal_odds: Decimal,
    correlation_known: bool,
    modeled_joint_probability: Decimal | None = None,
) -> dict[str, Any]:
    if modeled_joint_probability is not None:
        joint = modeled_joint_probability
        method = "modeled_correlation"
    elif correlation_known:
        joint = independent_joint_probability(probabilities)
        method = "independent"
    else:
        return {
            "status": "pass",
            "reason": "important correlation could not be measured responsibly",
            "strategy_version": STRATEGY_VERSION,
        }
    be = break_even_probability(parlay_decimal_odds)
    ev = expected_value(joint, parlay_decimal_odds)
    return {
        "status": "test_only_selection" if ev > 0 else "pass",
        "joint_probability": str(joint),
        "break_even_probability": str(be),
        "ev": str(ev),
        "method": method,
        "strategy_version": STRATEGY_VERSION,
    }


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return D(str(value))
    except Exception:
        return D(default)


def _rows_from_evaluation(evaluation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("candidates", "candidate_results", "evaluations", "test_only_selections"):
        rows = evaluation.get(key)
        if isinstance(rows, list) and rows:
            return [row for row in rows if isinstance(row, Mapping)]
    return []


def expressions_from_evaluation(evaluation: Mapping[str, Any]) -> list[MarketExpression]:
    rows = _rows_from_evaluation(evaluation)
    out: list[MarketExpression] = []
    for idx, row in enumerate(rows):
        odds = _dec(row.get("decimal_odds"), "1")
        if odds <= 1:
            continue
        conservative = _dec(
            row.get("conservative_probability", row.get("conservative_cover_probability", row.get("model_probability", 0)))
        )
        if conservative > 1:
            conservative /= D("100")
        be = _dec(row.get("break_even_probability"), "0") or break_even_probability(odds)
        ev = _dec(row.get("ev_percentage", row.get("ev", row.get("expected_value", 0))))
        if abs(ev) > 1:
            ev /= D("100")
        edge = _dec(row.get("probability_edge", row.get("edge", conservative - be)))
        if abs(edge) > 1:
            edge /= D("100")
        uncertainty = _dec(row.get("uncertainty_margin"), "0")
        pscore = protection_score(probability=conservative, uncertainty_margin=uncertainty)
        out.append(
            MarketExpression(
                key=str(row.get("selection_id", row.get("prediction_id", row.get("game_id", idx)))),
                market=str(row.get("market_key", row.get("market", row.get("market_type", "unknown")))),
                decimal_odds=odds,
                conservative_probability=conservative,
                break_even_probability=be,
                ev=ev,
                probability_edge=edge,
                uncertainty_margin=uncertainty,
                information_quality=str(row.get("information_quality", "unknown")),
                stability_status=str(row.get("stability_status", "unknown")),
                protection_score=pscore,
                source=row,
            )
        )
    return out


def apply_best_market_expression_strategy(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    rows = _rows_from_evaluation(evaluation)
    expressions = expressions_from_evaluation(evaluation)
    best = choose_best_market_expression(expressions)
    generic_scan = scan_market_expressions(rows)
    return {
        "strategy_version": STRATEGY_VERSION,
        "mode": "test_only",
        "scope": "sport_agnostic",
        "candidate_count": len(expressions),
        "qualifying_count": sum(1 for item in expressions if item.qualifies),
        "best_market_expression": None if best is None else {
            "key": best.key,
            "market": best.market,
            "decimal_odds": str(best.decimal_odds),
            "conservative_probability": str(best.conservative_probability),
            "break_even_probability": str(best.break_even_probability),
            "probability_edge": str(best.probability_edge),
            "ev": str(best.ev),
            "protection_score": str(best.protection_score),
            "rank_score": str(rank_score(best)),
        },
        "expression_scan": generic_scan,
        "notice": (
            "Best Market Expression v2 is sport-agnostic. It evaluates every verified market expression supplied "
            "by a sport adapter, but it never invents missing derivative probabilities and remains test-only."
        ),
    }
