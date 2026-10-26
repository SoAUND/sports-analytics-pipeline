"""Strict MLB Phase 3 moneyline evaluation and ranking.

This module applies Our Pick Evaluation and Scoring System to pregame full-game
MLB moneylines.  It never invents a model probability, never treats a page
reachability check as player-level confirmation, and never produces an official
pick while the runtime remains test_only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import combinations
import json
import sqlite3
from typing import Any, Iterable

from .calculations import (
    DEFAULT_EDGE,
    ONE,
    ZERO,
    as_decimal,
    break_even_probability,
    minimum_acceptable_decimal_odds,
    remove_vig,
    standard_ev,
)
from .revised_strategy import publication_metadata
from .pick_grades import format_pick_strength, pick_strength_grade


RULE_VERSION = "our-mlb-moneyline-evaluation-v1.1"
DEFAULT_WINDOW_HOURS = 24
DEVELOPMENT_REQUIRED_EDGE = Decimal("0.05")
DEVELOPMENT_HIGH_CONFIDENCE_SCORE = 65
DEVELOPMENT_UNCERTAINTY_CAP = Decimal("0.10")
MINIMUM_ELIGIBLE_DECIMAL_ODDS = Decimal("1") + (Decimal("100") / Decimal("450"))


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evaluation timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _median(values: Iterable[Decimal]) -> Decimal | None:
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


@dataclass(frozen=True)
class MlbEvaluationPolicy:
    required_edge: Decimal = DEFAULT_EDGE
    minimum_score: int = 75
    minimum_oos_decisions: int = 100
    stale_after_minutes: int = 60
    development_required_edge: Decimal = DEVELOPMENT_REQUIRED_EDGE
    development_high_confidence_score: int = DEVELOPMENT_HIGH_CONFIDENCE_SCORE
    development_uncertainty_cap: Decimal = DEVELOPMENT_UNCERTAINTY_CAP
    minimum_eligible_decimal_odds: Decimal = MINIMUM_ELIGIBLE_DECIMAL_ODDS


@dataclass(frozen=True)
class MlbCandidateInput:
    game_id: str
    season_type: str
    team_id: str
    opponent_team_id: str
    sportsbook_key: str
    odds_snapshot_id: int
    decimal_odds: Decimal
    checked_at: datetime
    no_vig_probability: Decimal | None
    no_vig_probability_low: Decimal | None
    no_vig_probability_high: Decimal | None
    consensus_book_count: int
    model_run_id: str
    model_version: str
    feature_version: str
    data_version: str
    model_rule_version: str
    training_cutoff: datetime
    model_probability: Decimal
    uncertainty_margin: Decimal
    model_status: str
    oos_decision_count: int
    calibration_status: str
    clv_status: str
    research_complete: bool
    market_match_confirmed: bool
    material_information_complete: bool
    information_quality: str
    conflict_status: str
    exposure_status: str
    stability_status: str
    manual_approval_status: str
    starting_pitchers_confirmed: bool
    lineups_confirmed: bool
    source_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MlbScoreBreakdown:
    edge: int
    ev: int
    model_validation: int
    information_quality: int
    market_quality: int
    stability: int

    @property
    def total(self) -> int:
        return sum(asdict(self).values())


@dataclass(frozen=True)
class MlbCandidateResult:
    game_id: str
    team_id: str
    opponent_team_id: str
    sportsbook_key: str
    odds_snapshot_id: int
    decimal_odds: Decimal
    checked_at: datetime
    model_run_id: str
    model_version: str
    feature_version: str
    data_version: str
    model_rule_version: str
    training_cutoff: datetime
    break_even_probability: Decimal
    no_vig_probability: Decimal | None
    no_vig_probability_low: Decimal | None
    no_vig_probability_high: Decimal | None
    model_probability: Decimal
    uncertainty_margin: Decimal
    conservative_probability: Decimal
    probability_edge: Decimal
    ev_per_dollar: Decimal
    minimum_acceptable_decimal_odds: Decimal
    gate_results: dict[str, bool]
    score_breakdown: MlbScoreBreakdown
    confidence_label: str
    decision_type: str
    pass_reason: str | None
    official_eligible: bool
    reasons: tuple[str, ...]
    loss_reasons: tuple[str, ...]
    source_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "game_id": self.game_id,
            "team_id": self.team_id,
            "opponent_team_id": self.opponent_team_id,
            "market_key": "moneylines",
            "market": f"{self.team_id} full-game moneyline",
            "sportsbook": self.sportsbook_key,
            "odds_snapshot_id": self.odds_snapshot_id,
            "decimal_odds": _decimal_text(self.decimal_odds),
            "checked_at": _iso(self.checked_at),
            "model_run_id": self.model_run_id,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "data_version": self.data_version,
            "model_rule_version": self.model_rule_version,
            "training_cutoff": _iso(self.training_cutoff),
            "break_even_probability": _decimal_text(self.break_even_probability),
            "no_vig_probability": _decimal_text(self.no_vig_probability),
            "no_vig_probability_range": [
                _decimal_text(self.no_vig_probability_low),
                _decimal_text(self.no_vig_probability_high),
            ],
            "model_probability": _decimal_text(self.model_probability),
            "uncertainty_margin": _decimal_text(self.uncertainty_margin),
            "conservative_probability": _decimal_text(self.conservative_probability),
            "probability_edge": _decimal_text(self.probability_edge),
            "ev_per_dollar": _decimal_text(self.ev_per_dollar),
            "minimum_acceptable_decimal_odds": _decimal_text(
                self.minimum_acceptable_decimal_odds
            ),
            "gates": self.gate_results,
            "score_breakdown": asdict(self.score_breakdown),
            "pick_strength_score": self.score_breakdown.total,
            "pick_strength_grade": pick_strength_grade(self.score_breakdown.total),
            "pick_strength_display": format_pick_strength(self.score_breakdown.total),
            "confidence": self.confidence_label,
            "decision": self.decision_type,
            "pass_reason": self.pass_reason,
            "official_eligible": self.official_eligible,
            **publication_metadata(self.gate_results, self.score_breakdown.total, self.confidence_label, validated=self.official_eligible, decision=self.decision_type),
            "supporting_reasons": list(self.reasons),
            "edge_loss_reasons": list(self.loss_reasons),
            "source_notes": list(self.source_notes),
        }


def _edge_score(edge: Decimal) -> int:
    if edge < Decimal("0.02"):
        return 0
    if edge < Decimal("0.03"):
        return 6
    if edge < Decimal("0.04"):
        return 8
    if edge < Decimal("0.05"):
        return 10
    if edge < Decimal("0.06"):
        return 12
    if edge < Decimal("0.07"):
        return 14
    if edge < Decimal("0.08"):
        return 16
    if edge < Decimal("0.10"):
        return 18
    return 20


def _ev_score(ev: Decimal) -> int:
    if ev <= ZERO:
        return 0
    if ev < Decimal("0.05"):
        return 2
    if ev < Decimal("0.10"):
        return 5
    if ev < Decimal("0.15"):
        return 7
    if ev < Decimal("0.25"):
        return 9
    return 10


def _model_score(count: int, calibration: str, clv: str) -> int:
    if count <= 0:
        return 0
    if count < 100:
        return 10
    if calibration != "acceptable":
        return 0
    if count >= 500 and clv in {"stable", "favorable"}:
        return 25
    if count >= 250 and clv in {"stable", "favorable"}:
        return 20
    return 15


def score_mlb_candidate(
    candidate: MlbCandidateInput, edge: Decimal, ev: Decimal
) -> MlbScoreBreakdown:
    information = {
        "full_official": 20,
        "complete_minor_uncertainty": 15,
        "projected": 10,
        "missing_or_conflicting": 0,
    }.get(candidate.information_quality, 0)
    market = (
        15
        if candidate.consensus_book_count >= 3
        else 10
        if candidate.consensus_book_count == 2
        else 5
        if candidate.consensus_book_count == 1
        else 0
    )
    stability = {
        "robust": 10,
        "conditional": 5,
        "fragile": 0,
        "unknown": 0,
    }.get(candidate.stability_status, 0)
    return MlbScoreBreakdown(
        edge=_edge_score(edge),
        ev=_ev_score(ev),
        model_validation=_model_score(
            candidate.oos_decision_count,
            candidate.calibration_status,
            candidate.clv_status,
        ),
        information_quality=information,
        market_quality=market,
        stability=stability,
    )


def evaluate_mlb_candidate(
    candidate: MlbCandidateInput,
    policy: MlbEvaluationPolicy = MlbEvaluationPolicy(),
) -> MlbCandidateResult:
    model_probability = as_decimal(candidate.model_probability)
    uncertainty = as_decimal(candidate.uncertainty_margin)
    development = candidate.oos_decision_count < policy.minimum_oos_decisions
    required_edge = max(Decimal("0.05"), policy.required_edge)
    effective_uncertainty = uncertainty
    if model_probability < ZERO or model_probability > ONE:
        raise ValueError("MLB model probability must be between 0 and 1")
    if uncertainty < ZERO or uncertainty > ONE:
        raise ValueError("MLB uncertainty margin must be between 0 and 1")
    conservative = max(ZERO, model_probability - effective_uncertainty)
    price = as_decimal(candidate.decimal_odds)
    break_even = break_even_probability(price)
    edge = conservative - break_even
    ev = standard_ev(conservative, price)
    try:
        minimum_price = minimum_acceptable_decimal_odds(
            conservative, required_edge
        )
        price_acceptable = price >= minimum_price
    except ValueError:
        minimum_price = Decimal("999")
        price_acceptable = False

    validation_eligible = (
        candidate.oos_decision_count >= policy.minimum_oos_decisions
        and candidate.calibration_status == "acceptable"
    )
    model_eligible = candidate.model_status != "rejected"
    gates = {
        "research": candidate.research_complete,
        "current_price": candidate.checked_at >= candidate.training_cutoff,
        "market_match": candidate.market_match_confirmed,
        "confluence": candidate.consensus_book_count >= 2,
        "confluence_complete": candidate.consensus_book_count >= 4,
        "material_information": candidate.material_information_complete,
        "starting_pitchers": candidate.starting_pitchers_confirmed,
        "lineups": candidate.lineups_confirmed,
        "model_eligibility": model_eligible,
        "conservative_probability": conservative > ZERO,
        "required_edge": edge >= required_edge,
        "positive_ev": ev > ZERO,
        "acceptable_price": price_acceptable,
        "odds_cutoff": Decimal("1") + Decimal("100") / Decimal("300") <= price <= Decimal("11"),
        "conflict_integrity": candidate.conflict_status == "clear",
        "exposure": candidate.exposure_status == "clear",
        "stability": candidate.stability_status != "fragile",
        "manual_approval": candidate.manual_approval_status == "approved",
    }
    scores = score_mlb_candidate(candidate, edge, ev)

    hard_gate_names = (
        "research",
        "current_price",
        "market_match",
        "confluence",
        "material_information",
        "starting_pitchers",
        "lineups",
        "model_eligibility",
        "conservative_probability",
        "required_edge",
        "positive_ev",
        "acceptable_price",
        "odds_cutoff",
        "conflict_integrity",
        "stability",
        "exposure",
    )
    failed_hard = [name for name in hard_gate_names if not gates[name]]
    reasons = (
        f"Conservative edge is {(edge * 100):.2f} percentage points.",
        f"Moneyline EV is {(ev * 100):.2f}% per dollar in test tracking.",
        f"Best trustworthy price is {price}; minimum acceptable is {minimum_price}.",
    )
    loss_reasons = tuple(f"Gate failed: {name}." for name in failed_hard)
    if not validation_eligible:
        loss_reasons += (
            f"MLB moneyline model has {candidate.oos_decision_count} qualifying out-of-sample decisions; at least {policy.minimum_oos_decisions} with acceptable calibration are required.",
        )

    if failed_hard:
        decision_type = "pass"
        pass_reason = loss_reasons[0]
    elif development:
        decision_type = "test_only_selection"
        pass_reason = None
    elif scores.total < policy.minimum_score:
        decision_type = "test_only_selection"
        pass_reason = None
    else:
        decision_type = "test_only_selection"
        pass_reason = None

    # The project runtime is test_only.  Even a fully validated candidate cannot
    # become an official real-money action through this Phase 3 evaluator.
    official_eligible = (validation_eligible and candidate.model_status == "approved" and scores.total >= 75 and all(gates[name] for name in hard_gate_names))
    if development and decision_type == "test_only_selection":
        confidence = "high_confidence_test" if edge >= Decimal("0.05") and scores.total >= policy.development_high_confidence_score else "test_candidate"
    elif scores.total >= 85 and all(gates[name] for name in hard_gate_names) and validation_eligible:
        confidence = "high_test_only"
    elif scores.total >= 75 and all(gates[name] for name in hard_gate_names):
        confidence = "medium_test_only"
    else:
        confidence = "below_threshold"

    return MlbCandidateResult(
        game_id=candidate.game_id,
        team_id=candidate.team_id,
        opponent_team_id=candidate.opponent_team_id,
        sportsbook_key=candidate.sportsbook_key,
        odds_snapshot_id=candidate.odds_snapshot_id,
        decimal_odds=price,
        checked_at=candidate.checked_at,
        model_run_id=candidate.model_run_id,
        model_version=candidate.model_version,
        feature_version=candidate.feature_version,
        data_version=candidate.data_version,
        model_rule_version=candidate.model_rule_version,
        training_cutoff=candidate.training_cutoff,
        break_even_probability=break_even,
        no_vig_probability=candidate.no_vig_probability,
        no_vig_probability_low=candidate.no_vig_probability_low,
        no_vig_probability_high=candidate.no_vig_probability_high,
        model_probability=model_probability,
        uncertainty_margin=effective_uncertainty,
        conservative_probability=conservative,
        probability_edge=edge,
        ev_per_dollar=ev,
        minimum_acceptable_decimal_odds=minimum_price,
        gate_results=gates,
        score_breakdown=scores,
        confidence_label=confidence,
        decision_type=decision_type,
        pass_reason=pass_reason,
        official_eligible=official_eligible,
        reasons=reasons,
        loss_reasons=loss_reasons,
        source_notes=candidate.source_notes,
    )


def _selection_rank(item: MlbCandidateResult) -> tuple[Any, ...]:
    return (
        item.score_breakdown.total,
        item.ev_per_dollar,
        item.probability_edge,
        item.score_breakdown.model_validation,
        item.score_breakdown.information_quality,
        item.score_breakdown.stability,
        item.decimal_odds,
        -item.uncertainty_margin,
    )


def select_best_mlb_straight_pick(
    selections: Iterable[MlbCandidateResult],
) -> dict[str, Any]:
    qualified = [
        item for item in selections if item.decision_type == "test_only_selection"
    ]
    if not qualified:
        return {
            "decision": "pass",
            "selection": None,
            "pass_reason": "Pass - no reliable MLB moneyline edge.",
        }
    strongest = max(qualified, key=_selection_rank)
    return {
        "decision": strongest.decision_type,
        "selection": strongest.to_dict(),
        "pass_reason": None,
        "notice": "Strongest qualified MLB test-only straight selection. No real-money action is taken.",
    }


def _independent_three_game_screen(
    legs: tuple[MlbCandidateResult, MlbCandidateResult, MlbCandidateResult],
) -> bool:
    game_ids = {leg.game_id for leg in legs}
    teams = {
        team
        for leg in legs
        for team in (leg.team_id, leg.opponent_team_id)
    }
    return len(game_ids) == 3 and len(teams) == 6


def build_best_mlb_three_leg_parlay(
    selections: Iterable[MlbCandidateResult],
    policy: MlbEvaluationPolicy = MlbEvaluationPolicy(),
) -> dict[str, Any]:
    qualified = [
        item for item in selections if item.decision_type == "test_only_selection"
    ]
    groups = [
        group
        for group in combinations(qualified, 3)
        if _independent_three_game_screen(group)
    ]
    if not groups:
        return {
            "decision": "pass",
            "proposed_legs": [],
            "pass_reason": "Fewer than three distinct qualified MLB games were available.",
        }

    def rank(group):
        return (
            min(item.score_breakdown.total for item in group),
            sum(item.score_breakdown.total for item in group),
            min(item.ev_per_dollar for item in group),
            sum(item.ev_per_dollar for item in group),
        )

    strongest = max(groups, key=rank)
    joint = ONE
    for leg in strongest:
        joint *= leg.conservative_probability
    try:
        minimum_parlay_price = minimum_acceptable_decimal_odds(
            joint, policy.required_edge
        )
    except ValueError:
        minimum_parlay_price = None

    return {
        "decision": "pass",
        "status": "proposed_test_only_legs_price_and_correlation_confirmation_required",
        "proposed_legs": [
            {
                "game_id": leg.game_id,
                "team_id": leg.team_id,
                "opponent_team_id": leg.opponent_team_id,
                "sportsbook": leg.sportsbook_key,
                "decimal_odds": _decimal_text(leg.decimal_odds),
                "conservative_probability": _decimal_text(leg.conservative_probability),
                "probability_edge": _decimal_text(leg.probability_edge),
                "ev_per_dollar": _decimal_text(leg.ev_per_dollar),
                "pick_strength_score": leg.score_breakdown.total,
                "pick_strength_grade": pick_strength_grade(leg.score_breakdown.total),
                "pick_strength_display": format_pick_strength(leg.score_breakdown.total),
            }
            for leg in strongest
        ],
        "joint_conservative_probability_assuming_independence": _decimal_text(joint),
        "minimum_acceptable_parlay_decimal_odds": _decimal_text(minimum_parlay_price),
        "actual_parlay_decimal_odds": None,
        "parlay_ev_per_dollar": None,
        "pass_reason": (
            "The actual sportsbook parlay price and material leg correlation were not verified. "
            "Our parlay rule therefore requires a pass."
        ),
        "notice": "Proposed test-only legs only. No real-money action is taken.",
    }


class MlbEvaluationEngine:
    """Evaluate stored MLB moneyline markets without fabricating missing research."""

    def __init__(self, db: sqlite3.Connection, policy: MlbEvaluationPolicy):
        self.db = db
        self.policy = policy

    def run(
        self,
        *,
        as_of: datetime,
        hours: int = DEFAULT_WINDOW_HOURS,
        game_id: str | None = None,
        game_ids: Iterable[str] | None = None,
        persist: bool = True,
    ) -> dict[str, Any]:
        end = as_of + timedelta(hours=hours)
        params: list[Any] = [_iso(as_of), _iso(end)]
        filters: list[str] = []
        if game_id:
            filters.append("game_id = ?")
            params.append(game_id)
        allowed = tuple(dict.fromkeys(str(value) for value in (game_ids or ())))
        if game_ids is not None:
            if not allowed:
                games = []
            else:
                placeholders = ",".join("?" for _ in allowed)
                filters.append(f"game_id IN ({placeholders})")
                params.extend(allowed)
                extra = "" if not filters else " AND " + " AND ".join(filters)
                games = self.db.execute(
                    f"""
                    SELECT * FROM games
                    WHERE sport='mlb' AND status='scheduled'
                      AND kickoff_time >= ? AND kickoff_time <= ?
                      {extra}
                    ORDER BY kickoff_time, game_id
                    """,
                    params,
                ).fetchall()
        else:
            extra = "" if not filters else " AND " + " AND ".join(filters)
            games = self.db.execute(
                f"""
                SELECT * FROM games
                WHERE sport='mlb' AND status='scheduled'
                  AND kickoff_time >= ? AND kickoff_time <= ?
                  {extra}
                ORDER BY kickoff_time, game_id
                """,
                params,
            ).fetchall()

        selections: list[MlbCandidateResult] = []
        passes: list[dict[str, Any]] = []
        paper_records: list[dict[str, Any]] = []
        for game in games:
            result = self._evaluate_game(game, as_of)
            if isinstance(result, MlbCandidateResult):
                if persist:
                    from .paper import record_paper_decision

                    paper_records.append(
                        record_paper_decision(
                            self.db,
                            game=game,
                            outcome=result.to_dict(),
                            decision_time=as_of,
                        )
                    )
                if result.decision_type == "test_only_selection":
                    selections.append(result)
                else:
                    item = result.to_dict()
                    item["qualification_status"] = "NO_QUALIFYING_PICK"
                    passes.append(item)
            else:
                if persist:
                    from .paper import record_paper_decision

                    paper_records.append(
                        record_paper_decision(
                            self.db, game=game, outcome=result, decision_time=as_of
                        )
                    )
                passes.append(result)

        selections.sort(key=_selection_rank, reverse=True)
        if selections:
            qualification_status = "QUALIFIED"
        elif passes and all(
            item.get("qualification_status") == "DATA_LIMITED" for item in passes
        ):
            qualification_status = "DATA_LIMITED"
        else:
            qualification_status = "NO_QUALIFYING_PICK"
        return {
            "evaluated_at": _iso(as_of),
            "window_hours": hours,
            "rule_version": RULE_VERSION,
            "mode": "research",
            "qualification_status": qualification_status,
            "games_found": len(games),
            "best_straight_pick": select_best_mlb_straight_pick(selections),
            "best_three_leg_parlay": build_best_mlb_three_leg_parlay(
                selections, self.policy
            ),
            "test_only_selections": [
                {**item.to_dict(), "qualification_status": "QUALIFIED"}
                for item in selections
            ],
            "passes": passes,
            "paper_records": paper_records,
            "official_picks": [item.to_dict() for item in selections if item.to_dict()["publication_decision"] == "official_pick"],
            "research_picks": [item.to_dict() for item in selections],
            "notice": (
                "MLB Phase 3 remains research/paper tracking only. Missing model, lineup, "
                "starter, source, price, or integrity evidence forces a pass."
            ),
        }

    def _latest_model(self, game_id: str, as_of: datetime):
        return self.db.execute(
            """
            SELECT * FROM model_runs
            WHERE game_id=? AND sport='mlb' AND market_key='moneylines'
              AND prediction_time <= ?
            ORDER BY prediction_time DESC, created_at DESC
            LIMIT 1
            """,
            (game_id, _iso(as_of)),
        ).fetchone()

    def _latest_context(self, game_id: str, cutoff: str) -> dict[str, Any]:
        row = self.db.execute(
            """
            SELECT * FROM evaluation_contexts
            WHERE game_id=? AND as_of <= ?
            ORDER BY as_of DESC, created_at DESC
            LIMIT 1
            """,
            (game_id, cutoff),
        ).fetchone()
        if row is None:
            return {
                "research_complete": 0,
                "market_match_confirmed": 0,
                "material_information_complete": 0,
                "information_quality": "missing_or_conflicting",
                "conflict_status": "unknown",
                "exposure_status": "unknown",
                "stability_status": "unknown",
                "manual_approval_status": "pending",
                "source_notes_json": json.dumps(
                    ["No reviewed MLB evaluation context existed by the model cutoff."]
                ),
            }
        return dict(row)

    def _mlb_context(self, game_id: str, cutoff: str):
        return self.db.execute(
            "SELECT * FROM mlb_game_context WHERE game_id=? AND collected_at <= ?",
            (game_id, cutoff),
        ).fetchone()

    def _official_checks_complete(self, game_id: str, cutoff: str) -> bool:
        required = {"injuries", "lineups", "transactions", "probable_pitchers"}
        rows = self.db.execute(
            """
            SELECT check_type, access_status, checked_at
            FROM source_checks
            WHERE game_id=? AND source_key LIKE 'mlb_%' AND checked_at <= ?
            ORDER BY checked_at DESC
            """,
            (game_id, cutoff),
        ).fetchall()
        latest: dict[str, sqlite3.Row] = {}
        for row in rows:
            latest.setdefault(row["check_type"], row)
        return all(
            check in latest and latest[check]["access_status"] == "confirmed"
            for check in required
        )

    def _market_pairs(self, game: sqlite3.Row, cutoff_dt: datetime):
        cutoff = _iso(cutoff_dt)
        rows = self.db.execute(
            """
            SELECT * FROM odds_snapshots
            WHERE game_id=? AND market_key='moneylines' AND period='full_game'
              AND market_status='open'
              AND source_updated_at <= ? AND collected_at <= ?
            ORDER BY source_updated_at DESC, collected_at DESC, odds_snapshot_id DESC
            """,
            (game["game_id"], cutoff, cutoff),
        ).fetchall()
        # Pair only quotes from the same sportsbook and exact source update.
        grouped: dict[tuple[str, str, str], dict[str, sqlite3.Row]] = {}
        for row in rows:
            key = (
                row["sportsbook_key"],
                row["source_updated_at"],
                row["settlement_rule"],
            )
            grouped.setdefault(key, {}).setdefault(row["outcome_team_id"], row)
        pairs = []
        for (_book, source_time, _settlement), sides in grouped.items():
            home = sides.get(game["home_team_id"])
            away = sides.get(game["away_team_id"])
            if home is None or away is None:
                continue
            age = cutoff_dt - datetime.fromisoformat(source_time)
            if age < timedelta(0) or age > timedelta(minutes=self.policy.stale_after_minutes):
                continue
            pairs.append((home, away))
        return pairs

    def _evaluate_game(
        self, game: sqlite3.Row, as_of: datetime
    ) -> MlbCandidateResult | dict[str, Any]:
        model = self._latest_model(game["game_id"], as_of)
        if model is None:
            return self._simple_pass(
                game, "No MLB moneyline model run existed by the evaluation time."
            )
        cutoff_dt = datetime.fromisoformat(model["prediction_time"])
        cutoff = _iso(cutoff_dt)
        pairs = self._market_pairs(game, cutoff_dt)
        trustworthy_pairs = [
            pair
            for pair in pairs
            if all(
                bool(row["is_accessible"]) or bool(row["is_consensus_eligible"])
                for row in pair
            )
        ]
        if not trustworthy_pairs:
            return self._simple_pass(
                game,
                "DATA_LIMITED - no fresh, complete, trustworthy two-sided MLB moneyline existed by the model cutoff.",
                qualification_status="DATA_LIMITED",
            )

        context = self._latest_context(game["game_id"], cutoff)
        mlb_context = self._mlb_context(game["game_id"], cutoff)
        if mlb_context is None:
            return self._simple_pass(game, "MLB game context is missing.")

        pitchers_confirmed = bool(
            mlb_context["confirmed_home_pitcher_id"]
            and mlb_context["confirmed_away_pitcher_id"]
        )
        lineups_confirmed = (
            str(mlb_context["home_lineup_status"] or "").casefold() == "confirmed"
            and str(mlb_context["away_lineup_status"] or "").casefold() == "confirmed"
        )
        official_checks = self._official_checks_complete(game["game_id"], cutoff)

        candidates: list[MlbCandidateResult] = []
        for side_index, team_id in enumerate(
            (game["home_team_id"], game["away_team_id"])
        ):
            opponent = (
                game["away_team_id"] if side_index == 0 else game["home_team_id"]
            )
            available = [
                pair[side_index]
                for pair in trustworthy_pairs
            ]
            best = max(
                available,
                key=lambda row: (row["decimal_odds"], row["source_updated_at"]),
            )
            consensus_probabilities: list[Decimal] = []
            for home, away in trustworthy_pairs:
                if not (
                    home["is_consensus_eligible"]
                    and away["is_consensus_eligible"]
                ):
                    continue
                probs = remove_vig((home["decimal_odds"], away["decimal_odds"]))
                consensus_probabilities.append(probs[side_index])
            if not consensus_probabilities:
                consensus_probabilities = [
                    remove_vig((home["decimal_odds"], away["decimal_odds"]))[side_index]
                    for home, away in trustworthy_pairs
                ]
            try:
                notes = tuple(json.loads(context["source_notes_json"]))
            except (TypeError, json.JSONDecodeError):
                notes = ("Evaluation context source notes were invalid JSON.",)
            if not official_checks:
                notes += (
                    "One or more required official MLB page checks were missing, stale, or unavailable by the model cutoff.",
                )

            candidate = MlbCandidateInput(
                game_id=game["game_id"],
                season_type=game["season_type"],
                team_id=team_id,
                opponent_team_id=opponent,
                sportsbook_key=best["sportsbook_key"],
                odds_snapshot_id=best["odds_snapshot_id"],
                decimal_odds=as_decimal(best["decimal_odds"]),
                checked_at=datetime.fromisoformat(best["source_updated_at"]),
                no_vig_probability=_median(consensus_probabilities),
                no_vig_probability_low=(
                    min(consensus_probabilities) if consensus_probabilities else None
                ),
                no_vig_probability_high=(
                    max(consensus_probabilities) if consensus_probabilities else None
                ),
                consensus_book_count=len(consensus_probabilities),
                model_run_id=model["model_run_id"],
                model_version=model["model_version"],
                feature_version=model["feature_version"],
                data_version=model["data_version"],
                model_rule_version=model["rule_version"],
                training_cutoff=datetime.fromisoformat(model["training_cutoff"]),
                # model_runs predates MLB support; for moneylines these two columns
                # store home/away win probability with push_probability fixed at 0.
                model_probability=as_decimal(
                    model["home_cover_probability"]
                    if side_index == 0
                    else model["away_cover_probability"]
                ),
                uncertainty_margin=as_decimal(model["uncertainty_margin"]),
                model_status=model["status"],
                oos_decision_count=int(model["oos_decision_count"]),
                calibration_status=model["calibration_status"],
                clv_status=model["clv_status"],
                research_complete=bool(context["research_complete"]) and official_checks,
                market_match_confirmed=bool(context["market_match_confirmed"]),
                material_information_complete=bool(
                    context["material_information_complete"]
                ),
                information_quality=context["information_quality"],
                conflict_status=context["conflict_status"],
                exposure_status=context["exposure_status"],
                stability_status=context["stability_status"],
                manual_approval_status=context["manual_approval_status"],
                starting_pitchers_confirmed=pitchers_confirmed,
                lineups_confirmed=lineups_confirmed,
                source_notes=notes,
            )
            candidates.append(evaluate_mlb_candidate(candidate, self.policy))

        if not candidates:
            return self._simple_pass(
                game,
                "DATA_LIMITED - no trustworthy sportsbook offered the evaluated moneyline.",
                qualification_status="DATA_LIMITED",
            )
        return max(
            candidates,
            key=lambda result: (
                result.decision_type == "test_only_selection",
                result.score_breakdown.total,
                result.ev_per_dollar,
                result.probability_edge,
            ),
        )

    @staticmethod
    def _simple_pass(
        game: sqlite3.Row,
        reason: str,
        *,
        qualification_status: str = "NO_QUALIFYING_PICK",
    ) -> dict[str, Any]:
        return {
            "game_id": game["game_id"],
            "market_key": "moneylines",
            "matchup": f"{game['away_team_id']} at {game['home_team_id']}",
            "kickoff_time": game["kickoff_time"],
            "decision": "pass",
            "qualification_status": qualification_status,
            "pass_reason": reason,
            "official_eligible": False,
        }


def evaluate_mlb_upcoming(
    db: sqlite3.Connection,
    *,
    as_of: datetime,
    hours: int = DEFAULT_WINDOW_HOURS,
    game_id: str | None = None,
    game_ids: Iterable[str] | None = None,
    stale_after_minutes: int = 60,
    persist: bool = True,
) -> dict[str, Any]:
    return MlbEvaluationEngine(
        db, MlbEvaluationPolicy(stale_after_minutes=stale_after_minutes)
    ).run(
        as_of=as_of, hours=hours, game_id=game_id, game_ids=game_ids, persist=persist
    )

