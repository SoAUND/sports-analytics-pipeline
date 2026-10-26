"""Test-only Best Market Expression scanner for every sport the bot can model.

The scanner is sport-agnostic. It compares different ways to express the same
pregame thesis without inventing probabilities. It accepts only probabilities
and prices supplied by our existing research/model pipeline and keeps same-event
parlay construction locked unless an explicit joint probability is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

D = Decimal
MARKET_EXPRESSION_VERSION = "best_market_expression_v2.1"

SUPPORTED_MARKET_EXPRESSIONS = {
    "moneyline": ("full_event", "Moneyline"),
    "moneylines": ("full_event", "Moneyline"),
    "h2h": ("full_event", "Moneyline"),
    "three_way_moneyline": ("full_event", "Three-Way Moneyline"),
    "draw_no_bet": ("full_event", "Draw No Bet"),
    "double_chance": ("full_event", "Double Chance"),
    "spreads": ("full_event", "Full-Event Spread"),
    "alternate_spreads": ("full_event", "Alternate Spread"),
    "totals": ("full_event", "Full-Event Total"),
    "alternate_totals": ("full_event", "Alternate Total"),
    "team_total": ("full_event", "Team Total"),
    "team_totals": ("full_event", "Team Total"),
    "alternate_team_total": ("full_event", "Alternate Team Total"),
    "first_half_spread": ("first_half", "First-Half Spread"),
    "spreads_h1": ("first_half", "First-Half Spread"),
    "first_half_total": ("first_half", "First-Half Total"),
    "totals_h1": ("first_half", "First-Half Total"),
    "first_quarter_spread": ("first_quarter", "First-Quarter Spread"),
    "spreads_q1": ("first_quarter", "First-Quarter Spread"),
    "first_quarter_total": ("first_quarter", "First-Quarter Total"),
    "totals_q1": ("first_quarter", "First-Quarter Total"),
    "first_period_spread": ("first_period", "First-Period Spread"),
    "first_period_total": ("first_period", "First-Period Total"),
    "first_five_spread": ("first_five", "First-Five Spread"),
    "first_five_total": ("first_five", "First-Five Total"),
    "set_spread": ("set", "Set Spread"),
    "set_total": ("set", "Set Total"),
    "game_spread": ("game", "Game Spread"),
    "game_total": ("game", "Game Total"),
    "round_total": ("rounds", "Round Total"),
    "fight_goes_distance": ("full_event", "Fight Goes Distance"),
    "method_of_victory": ("full_event", "Method of Victory"),
    "map_spread": ("map", "Map Spread"),
    "map_total": ("map", "Map Total"),
    "player_prop": ("full_event", "Player Prop"),
    "player_props": ("full_event", "Player Prop"),
    "participant_prop": ("full_event", "Participant Prop"),
    "race_prop": ("full_event", "Race Prop"),
}

PERIOD_LABELS = {
    "full_game": "Full-Game",
    "full_match": "Full-Match",
    "full_fight": "Full-Fight",
    "full_race": "Full-Race",
    "full_event": "Full-Event",
    "first_half": "First-Half",
    "second_half": "Second-Half",
    "first_quarter": "First-Quarter",
    "first_period": "First-Period",
    "first_five": "First-Five",
    "set": "Set",
    "game": "Game",
    "rounds": "Rounds",
    "map": "Map",
}


@dataclass(frozen=True)
class MarketExpressionCandidate:
    key: str
    sport: str
    event_id: str
    thesis_key: str
    market_key: str
    period: str
    label: str
    decimal_odds: Decimal
    conservative_probability: Decimal
    probability_edge: Decimal
    ev: Decimal
    uncertainty_margin: Decimal = D("0")
    research_clear: bool = False
    stability_clear: bool = False
    correlation_group: str | None = None
    source: Mapping[str, Any] | None = None

    @property
    def eligible(self) -> bool:
        return (
            D("1.25") <= self.decimal_odds <= D("4.00")
            and self.conservative_probability >= D("0.55")
            and self.research_clear
            and self.stability_clear
        )

    @property
    def rank_score(self) -> Decimal:
        if not self.eligible:
            return D("-999")
        probability_component = self.conservative_probability * D("70")
        edge_component = max(D("-0.08"), min(D("0.10"), self.probability_edge)) * D("20")
        ev_component = max(D("-0.10"), min(D("0.15"), self.ev)) * D("12")
        uncertainty_penalty = self.uncertainty_margin * D("25")
        return probability_component + edge_component + ev_component - uncertainty_penalty


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        return D(str(value))
    except Exception:
        return D(default)


def _generic_definition(key: str, period: str) -> tuple[str, str] | None:
    normalized_period = period or "full_event"
    prefix = PERIOD_LABELS.get(normalized_period, normalized_period.replace("_", " ").title())
    if "spread" in key or "handicap" in key or "run_line" in key or "puck_line" in key:
        return normalized_period, f"{prefix} Spread"
    if "team_total" in key:
        return normalized_period, f"{prefix} Team Total"
    if "total" in key or "over_under" in key:
        return normalized_period, f"{prefix} Total"
    if any(token in key for token in ("moneyline", "h2h", "winner", "match_winner", "fight_winner")):
        return normalized_period, f"{prefix} Moneyline"
    if any(token in key for token in ("prop", "points", "rebounds", "assists", "yards", "receptions", "strikeouts", "hits", "goals", "shots", "aces")):
        return normalized_period, f"{prefix} Prop"
    return None


def market_expression_definition(market_key: str, period: str | None = None) -> tuple[str, str] | None:
    key = str(market_key or "").casefold().strip()
    definition = SUPPORTED_MARKET_EXPRESSIONS.get(key)
    if definition is not None:
        return definition
    normalized_period = str(period or "full_event").casefold().strip() or "full_event"
    if key in {"spread", "total"}:
        prefix = PERIOD_LABELS.get(normalized_period, normalized_period.replace("_", " ").title())
        return normalized_period, f"{prefix} {key.title()}"
    return _generic_definition(key, normalized_period)


def candidates_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[MarketExpressionCandidate]:
    candidates: list[MarketExpressionCandidate] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        market_key = str(row.get("market_key") or row.get("market") or row.get("market_type") or "")
        period = str(row.get("period") or row.get("market_period") or "")
        definition = market_expression_definition(market_key, period)
        if definition is None:
            continue
        normalized_period, label = definition
        odds = _dec(row.get("decimal_odds"))
        probability = _dec(
            row.get(
                "conservative_probability",
                row.get("conservative_cover_probability", row.get("model_probability", 0)),
            )
        )
        if probability > 1:
            probability /= D("100")
        edge = _dec(row.get("probability_edge", row.get("edge", 0)))
        if abs(edge) > 1:
            edge /= D("100")
        ev = _dec(row.get("ev", row.get("expected_value", row.get("ev_percentage", 0))))
        if abs(ev) > 1:
            ev /= D("100")
        event_id = str(row.get("event_id") or row.get("game_id") or row.get("match_id") or row.get("fight_id") or f"event-{index}")
        outcome = str(row.get("selection_name") or row.get("outcome_role") or row.get("side") or "selection")
        thesis_key = str(row.get("thesis_key") or f"{event_id}:{outcome.casefold()}")
        research_clear = bool(row.get("research_clear", False)) or str(row.get("information_quality") or "") in {
            "complete", "complete_current", "full_official", "high", "complete_minor_uncertainty"
        }
        stability_clear = bool(row.get("stability_clear", False)) or str(row.get("stability_status") or "") in {
            "stable", "passed", "robust", "conditional"
        }
        candidates.append(
            MarketExpressionCandidate(
                key=str(row.get("selection_id") or row.get("prediction_id") or f"expression-{index}"),
                sport=str(row.get("sport") or row.get("league") or "unknown").casefold(),
                event_id=event_id,
                thesis_key=thesis_key,
                market_key=market_key,
                period=normalized_period,
                label=label,
                decimal_odds=odds,
                conservative_probability=probability,
                probability_edge=edge,
                ev=ev,
                uncertainty_margin=_dec(row.get("uncertainty_margin")),
                research_clear=research_clear,
                stability_clear=stability_clear,
                correlation_group=(str(row.get("correlation_group")) if row.get("correlation_group") else None),
                source=row,
            )
        )
    return candidates


def rank_market_expressions(candidates: Sequence[MarketExpressionCandidate]) -> list[MarketExpressionCandidate]:
    return sorted(
        (candidate for candidate in candidates if candidate.eligible),
        key=lambda candidate: (
            candidate.rank_score,
            candidate.conservative_probability,
            candidate.ev,
            candidate.decimal_odds,
        ),
        reverse=True,
    )


def best_expression_by_thesis(candidates: Sequence[MarketExpressionCandidate]) -> list[MarketExpressionCandidate]:
    winners: dict[str, MarketExpressionCandidate] = {}
    for candidate in rank_market_expressions(candidates):
        winners.setdefault(candidate.thesis_key, candidate)
    return list(winners.values())


def scan_market_expressions(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    candidates = candidates_from_rows(rows)
    ranked = rank_market_expressions(candidates)
    best = best_expression_by_thesis(candidates)
    sports = sorted({candidate.sport for candidate in candidates if candidate.sport and candidate.sport != "unknown"})
    return {
        "version": MARKET_EXPRESSION_VERSION,
        "mode": "test_only",
        "scope": "sport_agnostic",
        "sports_seen": sports,
        "candidate_count": len(candidates),
        "eligible_count": len(ranked),
        "supported_expressions": sorted({label for _, label in SUPPORTED_MARKET_EXPRESSIONS.values()}),
        "ranked": [
            {
                "key": candidate.key,
                "sport": candidate.sport,
                "event_id": candidate.event_id,
                "thesis_key": candidate.thesis_key,
                "market_key": candidate.market_key,
                "period": candidate.period,
                "label": candidate.label,
                "decimal_odds": str(candidate.decimal_odds),
                "conservative_probability": str(candidate.conservative_probability),
                "probability_edge": str(candidate.probability_edge),
                "ev": str(candidate.ev),
                "rank_score": str(candidate.rank_score),
            }
            for candidate in ranked
        ],
        "best_by_thesis": [candidate.key for candidate in best],
        "same_event_parlay_rule": "blocked_without_explicit_joint_probability",
        "notice": (
            "The scanner is not limited to college football. It can compare moneylines, spreads, alternate lines, "
            "totals, team totals, period markets, set/game/map/round markets, and participant props for any sport "
            "when that sport's existing adapter supplies a verified price and conservative probability. It never "
            "invents derivative probabilities."
        ),
    }
