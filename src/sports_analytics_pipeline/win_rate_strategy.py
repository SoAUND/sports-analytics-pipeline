"""Test-only Win-Rate-First strategy lane.

This lane keeps the existing expected-value evaluator intact. It prioritizes
higher-probability researched selections priced from -400 through +300 American
odds, scans available market expressions for the same event thesis, then builds
2-4 leg test tickets only when the combined decimal price is at least 4.00.

It never takes a real-money action, never invents a probability, and never overrides
research-integrity or market-integrity gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations
from math import prod
from typing import Any, Iterable, Mapping, Sequence

from .market_expression import scan_market_expressions

D = Decimal
STRATEGY_VERSION = "win_rate_first_v1.2"
MIN_DECIMAL_ODDS = D("1.25")  # -400 American
MAX_DECIMAL_ODDS = D("4.00")  # +300 American
MIN_CONSERVATIVE_PROBABILITY = D("0.55")
PREMIUM_PROBABILITY = D("0.70")
STRONG_PROBABILITY = D("0.60")
MIN_TICKET_DECIMAL_ODDS = D("4.00")  # 4x total return / +300
MAX_PARLAY_LEGS = 4
MIN_PARLAY_LEGS = 2

RESEARCH_ELIGIBLE = {
    "complete",
    "complete_current",
    "full_official",
    "high",
    "complete_minor_uncertainty",
}
STABILITY_ELIGIBLE = {"stable", "passed", "robust", "conditional"}


@dataclass(frozen=True)
class WinRateCandidate:
    key: str
    market: str
    decimal_odds: Decimal
    conservative_probability: Decimal
    break_even_probability: Decimal
    ev: Decimal
    probability_edge: Decimal
    information_quality: str = "unknown"
    stability_status: str = "unknown"
    uncertainty_margin: Decimal = D("0")
    source: Mapping[str, Any] | None = None

    @property
    def odds_eligible(self) -> bool:
        return MIN_DECIMAL_ODDS <= self.decimal_odds <= MAX_DECIMAL_ODDS

    @property
    def probability_eligible(self) -> bool:
        return self.conservative_probability >= MIN_CONSERVATIVE_PROBABILITY

    @property
    def research_eligible(self) -> bool:
        return self.information_quality in RESEARCH_ELIGIBLE

    @property
    def stability_eligible(self) -> bool:
        return self.stability_status in STABILITY_ELIGIBLE

    @property
    def candidate_eligible(self) -> bool:
        return self.odds_eligible and self.probability_eligible and self.research_eligible and self.stability_eligible

    @property
    def value_positive(self) -> bool:
        return self.ev > 0 and self.probability_edge > 0

    @property
    def probability_band(self) -> str:
        return probability_band(self.conservative_probability)

    @property
    def price_quality(self) -> str:
        return price_quality(self.ev, self.probability_edge)


def break_even_probability(decimal_odds: Decimal) -> Decimal:
    if decimal_odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return D("1") / decimal_odds


def expected_value(probability: Decimal, decimal_odds: Decimal) -> Decimal:
    return probability * decimal_odds - D("1")


def decimal_to_american(decimal_odds: Decimal) -> int:
    if decimal_odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    if decimal_odds >= 2:
        return int((decimal_odds - D("1")) * D("100"))
    return int(-(D("100") / (decimal_odds - D("1"))))


def probability_band(probability: Decimal) -> str:
    if probability >= PREMIUM_PROBABILITY:
        return "premium"
    if probability >= STRONG_PROBABILITY:
        return "strong"
    if probability >= MIN_CONSERVATIVE_PROBABILITY:
        return "selective"
    return "below_lane"


def price_quality(ev: Decimal, edge: Decimal) -> str:
    if ev > 0 and edge >= D("0.05"):
        return "strong_value"
    if ev > 0 and edge > 0:
        return "positive_value"
    if edge >= D("-0.02"):
        return "slightly_expensive"
    return "expensive"


def win_rate_display_fields(*, decimal_odds: Any, conservative_probability: Any, probability_edge: Any, ev: Any, research_complete: bool, stability_clear: bool = True) -> dict[str, Any]:
    odds = _dec(decimal_odds, "0")
    probability = _dec(conservative_probability, "0")
    edge = _dec(probability_edge, "0")
    expected = _dec(ev, "0")
    odds_eligible = MIN_DECIMAL_ODDS <= odds <= MAX_DECIMAL_ODDS
    probability_eligible = probability >= MIN_CONSERVATIVE_PROBABILITY
    lane_candidate = odds_eligible and probability_eligible
    safeguards_clear = research_complete and stability_clear
    status = "test_candidate" if lane_candidate and safeguards_clear else ("watch_only" if lane_candidate else "pass")
    return {
        "strategy_version": STRATEGY_VERSION,
        "odds_eligible": odds_eligible,
        "probability_eligible": probability_eligible,
        "probability_band": probability_band(probability),
        "price_quality": price_quality(expected, edge),
        "value_positive": expected > 0 and edge > 0,
        "research_safeguard_clear": bool(research_complete),
        "stability_safeguard_clear": bool(stability_clear),
        "status": status,
    }


def independent_joint_probability(probabilities: Sequence[Decimal]) -> Decimal:
    if not probabilities:
        raise ValueError("at least one probability is required")
    if any(p < 0 or p > 1 for p in probabilities):
        raise ValueError("probabilities must be between 0 and 1")
    return D(str(prod(float(p) for p in probabilities)))


def candidate_rank(candidate: WinRateCandidate) -> Decimal:
    if not candidate.candidate_eligible:
        return D("-999")
    info = {
        "complete": D("1"), "complete_current": D("1"), "full_official": D("1"),
        "high": D("0.9"), "complete_minor_uncertainty": D("0.8"),
    }.get(candidate.information_quality, D("0"))
    stability = D("1") if candidate.stability_status in {"stable", "passed", "robust"} else D("0.65")
    value_bonus = max(D("-0.10"), min(D("0.10"), candidate.probability_edge))
    return candidate.conservative_probability * D("60") + value_bonus * D("20") + info * D("10") + stability * D("10") - candidate.uncertainty_margin * D("25")


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


def candidates_from_evaluation(evaluation: Mapping[str, Any]) -> list[WinRateCandidate]:
    out: list[WinRateCandidate] = []
    for idx, row in enumerate(_rows_from_evaluation(evaluation)):
        odds = _dec(row.get("decimal_odds"), "0")
        if odds <= 1:
            continue
        conservative = _dec(row.get("conservative_probability", row.get("conservative_cover_probability", row.get("model_probability", 0))))
        if conservative > 1:
            conservative /= D("100")
        be = _dec(row.get("break_even_probability"), "0")
        if be <= 0:
            be = break_even_probability(odds)
        ev = _dec(row.get("ev", row.get("ev_percentage", row.get("expected_value", 0))))
        if abs(ev) > 1:
            ev /= D("100")
        edge = _dec(row.get("probability_edge", row.get("edge", conservative - be)))
        if abs(edge) > 1:
            edge /= D("100")
        out.append(WinRateCandidate(
            key=str(row.get("selection_id", row.get("prediction_id", row.get("game_id", idx)))),
            market=str(row.get("market_key", row.get("market", row.get("market_type", "unknown")))),
            decimal_odds=odds,
            conservative_probability=conservative,
            break_even_probability=be,
            ev=ev,
            probability_edge=edge,
            information_quality=str(row.get("information_quality", "unknown")),
            stability_status=str(row.get("stability_status", "unknown")),
            uncertainty_margin=_dec(row.get("uncertainty_margin"), "0"),
            source=row,
        ))
    return out


def _same_event(a: WinRateCandidate, b: WinRateCandidate) -> bool:
    if not a.source or not b.source:
        return False
    for key in ("event_id", "game_id", "match_id"):
        av, bv = a.source.get(key), b.source.get(key)
        if av is not None and bv is not None and str(av) == str(bv):
            return True
    return False


def _parlay_from_candidates(legs: Sequence[WinRateCandidate]) -> dict[str, Any] | None:
    for a, b in combinations(legs, 2):
        if _same_event(a, b):
            return None
    joint_probability = independent_joint_probability([leg.conservative_probability for leg in legs])
    decimal_odds = D("1")
    for leg in legs:
        decimal_odds *= leg.decimal_odds
    if decimal_odds < MIN_TICKET_DECIMAL_ODDS:
        return None
    be = break_even_probability(decimal_odds)
    ev = expected_value(joint_probability, decimal_odds)
    return {
        "legs": [leg.key for leg in legs],
        "leg_count": len(legs),
        "joint_probability": str(joint_probability),
        "decimal_odds": str(decimal_odds),
        "american_odds": decimal_to_american(decimal_odds),
        "break_even_probability": str(be),
        "ev": str(ev),
        "return_multiple": str(decimal_odds),
        "minimum_return_multiple": str(MIN_TICKET_DECIMAL_ODDS),
        "meets_minimum_4x": True,
        "price_status": "positive_value" if ev > 0 else "expensive",
        "correlation_method": "independent_different_events",
    }


def build_win_rate_parlays(candidates: Sequence[WinRateCandidate]) -> list[dict[str, Any]]:
    """Return one best ticket at the smallest leg count that reaches 4x.

    Same-event combinations remain blocked without a validated joint model.
    """
    eligible = sorted((c for c in candidates if c.candidate_eligible), key=lambda c: (candidate_rank(c), c.conservative_probability), reverse=True)
    pool = eligible[:8]
    for leg_count in range(MIN_PARLAY_LEGS, min(MAX_PARLAY_LEGS, len(pool)) + 1):
        qualified: list[dict[str, Any]] = []
        for legs in combinations(pool, leg_count):
            result = _parlay_from_candidates(legs)
            if result is not None:
                qualified.append(result)
        if qualified:
            qualified.sort(key=lambda row: (D(str(row["joint_probability"])), D(str(row["ev"])), -D(str(row["decimal_odds"]))), reverse=True)
            return [qualified[0]]
    return []


def apply_win_rate_first_strategy(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    rows = _rows_from_evaluation(evaluation)
    candidates = candidates_from_evaluation(evaluation)
    eligible = [candidate for candidate in candidates if candidate.candidate_eligible]
    ranked = sorted(eligible, key=lambda c: (candidate_rank(c), c.conservative_probability), reverse=True)
    cards = build_win_rate_parlays(ranked)
    expression_scan = scan_market_expressions(rows)
    return {
        "strategy_version": STRATEGY_VERSION,
        "mode": "test_only",
        "odds_range_american": {"min_favorite": -400, "max_underdog": 300},
        "minimum_conservative_probability": str(MIN_CONSERVATIVE_PROBABILITY),
        "minimum_ticket_decimal_odds": str(MIN_TICKET_DECIMAL_ODDS),
        "minimum_ticket_total_return_multiple": "4.00",
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "ranked_candidates": [{
            "key": c.key, "market": c.market, "decimal_odds": str(c.decimal_odds),
            "american_odds": decimal_to_american(c.decimal_odds),
            "conservative_probability": str(c.conservative_probability),
            "probability_band": c.probability_band, "break_even_probability": str(c.break_even_probability),
            "probability_edge": str(c.probability_edge), "ev": str(c.ev),
            "price_quality": c.price_quality, "value_positive": c.value_positive,
            "information_quality": c.information_quality, "stability_status": c.stability_status,
            "rank_score": str(candidate_rank(c)), "status": "win_rate_candidate",
        } for c in ranked[:12]],
        "market_expression_scan": expression_scan,
        "parlay_cards": cards,
        "ticket_status": "4x_ticket_ready" if cards else "no_4x_ticket",
        "notice": (
            "Win-Rate-First v1.2 scans available market expressions before building a ticket, requires at least "
            "4.00 decimal (+300) combined odds, prefers the fewest strong legs, never forces a ticket, remains "
            "test-only, and does not override the existing EV lane."
        ),
    }
