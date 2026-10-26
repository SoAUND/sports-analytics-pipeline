"""Shared pick/pass, scoring, ranking, and parlay engine for our new sports.

The engine implements the project's existing five-point conservative edge rule,
positive-EV rule, minimum price, research gate, market matching, settlement-rule
verification, independent validation buckets, and manual approval requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
from statistics import median
from typing import Mapping
from uuid import uuid4

from .calculations import (
    break_even_probability,
    minimum_acceptable_decimal_odds,
    push_aware_ev,
    remove_vig,
    standard_ev,
)
from .multisport_research import research_gate
from .multisport_runtime import (
    approval_status,
    ensure_runtime_schema,
    feature_vector,
    validation_count,
)
from .sports_registry import get_sport_market, validation_bucket


UTC = timezone.utc
STRICT_REQUIRED_EDGE = Decimal("0.05")
DEVELOPMENT_REQUIRED_EDGE = Decimal("0.05")
REQUIRED_EDGE = STRICT_REQUIRED_EDGE
MINIMUM_PICK_STRENGTH = 75
DEVELOPMENT_HIGH_CONFIDENCE_SCORE = 65
DEVELOPMENT_UNCERTAINTY_CAP = 0.10
MINIMUM_ELIGIBLE_DECIMAL_ODDS = Decimal("1") + (Decimal("100") / Decimal("450"))
DEFAULT_PAPER_UNIT_DOLLARS = 10.0
PAPER_UNITS_PER_SELECTION = 1.0
MAX_ACTIVE_SELECTIONS = 6
MAX_ACTIVE_SELECTIONS_PER_SPORT = 2
MAX_ACTIVE_SELECTIONS_PER_PARTICIPANT = 1
EXPOSURE_WINDOW_HOURS = 24
RULE_VERSION = "sap-revised-2026-10-01"


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class MarketView:
    sportsbook_key: str
    rows: tuple[sqlite3.Row, ...]
    probabilities: dict[str, float]
    collected_at: datetime


@dataclass(frozen=True)
class EvaluationResult:
    paper_decision_id: str
    decision_type: str
    outcome_role: str | None
    sportsbook_key: str | None
    decimal_odds: float | None
    probability_edge: float | None
    ev_per_dollar: float | None
    pick_strength_score: int | None
    letter_grade: str | None
    paper_units: float
    paper_unit_dollars: float
    paper_risk_dollars: float
    profit_if_win_dollars: float | None
    total_return_if_win_dollars: float | None
    confidence_label: str
    pass_reason: str | None


@dataclass(frozen=True)
class ExposureAssessment:
    clear: bool
    blockers: tuple[str, ...]
    active_total: int
    active_for_sport: int
    active_for_participant: int


CORE_GATES = (
    "research",
    "current_price",
    "market_match",
    "confluence",
    "material_information",
    "conservative_probability",
    "required_edge",
    "positive_ev",
    "acceptable_price",
    "odds_cutoff",
    "pick_strength",
    "conflict_and_integrity",
    "exposure",
)


def letter_grade(score: int | None) -> str | None:
    """Translate our 100-point pick-strength score into an A+ through F- grade."""
    if score is None:
        return None
    if not 0 <= score <= 100:
        raise ValueError("Pick-strength score must be between 0 and 100")
    thresholds = (
        (97, "A+"), (93, "A"), (90, "A-"),
        (87, "B+"), (83, "B"), (80, "B-"),
        (77, "C+"), (73, "C"), (70, "C-"),
        (67, "D+"), (63, "D"), (60, "D-"),
        (57, "F+"), (53, "F"), (0, "F-"),
    )
    return next(grade for minimum, grade in thresholds if score >= minimum)


def american_odds(decimal_odds: float | None) -> int | None:
    if decimal_odds is None:
        return None
    if decimal_odds <= 1:
        raise ValueError("Decimal odds must be greater than 1")
    if decimal_odds >= 2:
        return round((decimal_odds - 1.0) * 100)
    return round(-100.0 / (decimal_odds - 1.0))


def decision_presentation(
    decision: Mapping[str, object],
    *,
    paper_unit_dollars: float = DEFAULT_PAPER_UNIT_DOLLARS,
) -> dict[str, object]:
    """Create the plain-language score, grade, odds, and paper-dollar result."""
    if paper_unit_dollars <= 0:
        raise ValueError("Paper-unit dollar amount must be greater than zero")
    decision_type = str(decision["decision_type"])
    selection = decision_type != "pass"
    score_value = decision.get("pick_strength_score")
    score = int(score_value) if score_value is not None else None
    decimal_value = decision.get("decimal_odds")
    decimal_odds = float(decimal_value) if decimal_value is not None else None
    units = PAPER_UNITS_PER_SELECTION if selection else 0.0
    risk = round(units * paper_unit_dollars, 2)
    profit = round(risk * (decimal_odds - 1.0), 2) if selection and decimal_odds else None
    total_return = round(risk * decimal_odds, 2) if selection and decimal_odds else None

    def percent(name: str) -> float | None:
        value = decision.get(name)
        return round(float(value) * 100.0, 2) if value is not None else None

    return {
        "decision_type": decision_type,
        "sport": decision.get("sport"),
        "event_id": decision.get("event_id"),
        "market_key": decision.get("market_key"),
        "period": decision.get("period"),
        "selection_name": decision.get("selection_name"),
        "outcome_role": decision.get("outcome_role"),
        "points": decision.get("points"),
        "sportsbook": decision.get("sportsbook_key"),
        "decimal_odds": decimal_odds,
        "american_odds": american_odds(decimal_odds),
        "model_probability_percent": percent("model_probability"),
        "conservative_probability_percent": percent("conservative_probability"),
        "break_even_probability_percent": percent("break_even_probability"),
        "edge_percentage_points": percent("probability_edge"),
        "expected_value_percent": percent("ev_per_dollar"),
        "pick_strength_score": score,
        "letter_grade": letter_grade(score),
        "qualifying_score": MINIMUM_PICK_STRENGTH,
        "score_qualifies": bool(score is not None and score >= MINIMUM_PICK_STRENGTH),
        "score_is_win_probability": False,
        "confidence_label": decision.get("confidence_label"),
        "countable_oos": bool(decision.get("countable_oos", 0)),
        "paper_units": units,
        "paper_unit_dollars": round(paper_unit_dollars, 2),
        "paper_risk_dollars": risk,
        "profit_if_win_dollars": profit,
        "total_return_if_win_dollars": total_return,
        "pass_reason": decision.get("pass_reason"),
    }


def _required_roles(sport: str) -> tuple[str, ...]:
    return ("home", "draw", "away") if sport == "epl" else ("home", "away")


def current_market_views(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    as_of: datetime,
    stale_after_minutes: int,
) -> tuple[MarketView, ...]:
    ensure_runtime_schema(connection)
    spec = get_sport_market(sport)
    rows = connection.execute(
        """
        SELECT * FROM multisport_odds_snapshots
        WHERE event_id=? AND sport=? AND market_key=? AND period=?
          AND market_status='open' AND collected_at<=?
        ORDER BY sportsbook_key, collected_at DESC, odds_snapshot_id DESC
        """,
        (event_id, sport, spec.internal_market_key, spec.period, _iso(as_of)),
    ).fetchall()
    by_book: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_book.setdefault(str(row["sportsbook_key"]), []).append(row)

    views: list[MarketView] = []
    required = set(_required_roles(sport))
    for book, book_rows in by_book.items():
        latest_time = max(_dt(str(row["collected_at"])) for row in book_rows)
        if as_of.astimezone(UTC) - latest_time > timedelta(minutes=stale_after_minutes):
            continue
        snapshot = tuple(row for row in book_rows if _dt(str(row["collected_at"])) == latest_time)
        roles = {str(row["outcome_role"]) for row in snapshot}
        if roles != required or len(snapshot) != len(required):
            continue
        role_to_row = {str(row["outcome_role"]): row for row in snapshot}
        ordered = [role_to_row[role] for role in _required_roles(sport)]
        prices = [Decimal(str(row["decimal_odds"])) for row in ordered]
        no_vig = remove_vig(prices)
        probabilities = {role: float(prob) for role, prob in zip(_required_roles(sport), no_vig)}
        if spec.provider_market_key == "spreads":
            home_points = float(role_to_row["home"]["points"])
            away_points = float(role_to_row["away"]["points"])
            if abs(home_points + away_points) > 1e-9:
                continue
        views.append(
            MarketView(
                sportsbook_key=book,
                rows=snapshot,
                probabilities=probabilities,
                collected_at=latest_time,
            )
        )
    return tuple(views)


def _settlement_verified(
    connection: sqlite3.Connection,
    *,
    sportsbook_key: str,
    sport: str,
) -> bool:
    spec = get_sport_market(sport)
    book_keys = (sportsbook_key,)
    if sportsbook_key in {"hardrockbet", "hardrockbet_fl"}:
        book_keys = ("hardrockbet", "hardrockbet_fl")
    for book_key in book_keys:
        row = connection.execute(
            """
            SELECT rule_status FROM multisport_settlement_rules
            WHERE sportsbook_key=? AND sport=? AND market_key=? AND period=?
            """,
            (book_key, sport, spec.internal_market_key, spec.period),
        ).fetchone()
        if row and row["rule_status"] == "verified":
            return True
    return False


def _engineering_baseline_model(
    connection: sqlite3.Connection,
    model_version: str,
) -> bool:
    row = connection.execute(
        "SELECT feature_version, parameters_json FROM multisport_model_artifacts WHERE model_version=?",
        (model_version,),
    ).fetchone()
    if row is None or str(row["feature_version"]) != "market-baseline-features-1.0":
        return False
    try:
        parameters = json.loads(str(row["parameters_json"]))
    except Exception:
        return False
    return parameters.get("engineering_market_baseline") is True


def _latest_research_statuses(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    as_of: datetime,
) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT category, verification_status, collected_at
        FROM multisport_research_checks
        WHERE event_id=? AND sport=? AND collected_at<=?
        ORDER BY collected_at DESC, research_check_id DESC
        """,
        (event_id, sport, _iso(as_of)),
    ).fetchall()
    latest: dict[str, str] = {}
    for row in rows:
        latest.setdefault(str(row["category"]), str(row["verification_status"]))
    return latest


def assess_exposure(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    outcome_participant_id: str,
    as_of: datetime,
    max_active_total: int = MAX_ACTIVE_SELECTIONS,
    max_active_per_sport: int = MAX_ACTIVE_SELECTIONS_PER_SPORT,
    max_active_per_participant: int = MAX_ACTIVE_SELECTIONS_PER_PARTICIPANT,
) -> ExposureAssessment:
    """Check our upcoming paper portfolio before another selection is allowed."""
    ensure_runtime_schema(connection)
    window_end = as_of.astimezone(UTC) + timedelta(hours=EXPOSURE_WINDOW_HOURS)
    rows = connection.execute(
        """
        SELECT d.sport, d.outcome_participant_id
        FROM multisport_paper_decisions d
        JOIN multisport_events e ON e.event_id=d.event_id
        LEFT JOIN multisport_paper_results r
          ON r.paper_decision_id=d.paper_decision_id
        WHERE d.event_id<>?
          AND d.decision_type IN ('official_pick','conditional_pick','test_only_selection')
          AND e.commence_time>?
          AND e.commence_time<=?
          AND r.paper_result_id IS NULL
        """,
        (event_id, _iso(as_of), _iso(window_end)),
    ).fetchall()
    active_total = len(rows)
    active_for_sport = sum(1 for row in rows if str(row["sport"]) == sport)
    active_for_participant = sum(
        1
        for row in rows
        if str(row["outcome_participant_id"] or "") == outcome_participant_id
    )
    blockers: list[str] = []
    if active_total >= max_active_total:
        blockers.append(
            f"24-hour portfolio limit reached ({active_total}/{max_active_total} active selections)"
        )
    if active_for_sport >= max_active_per_sport:
        blockers.append(
            f"24-hour {sport.upper()} limit reached "
            f"({active_for_sport}/{max_active_per_sport} active selections)"
        )
    if active_for_participant >= max_active_per_participant:
        blockers.append(
            "participant already has an active selection "
            f"({active_for_participant}/{max_active_per_participant})"
        )
    return ExposureAssessment(
        clear=not blockers,
        blockers=tuple(blockers),
        active_total=active_total,
        active_for_sport=active_for_sport,
        active_for_participant=active_for_participant,
    )


def _failed_gate_reason(
    gates: Mapping[str, bool],
    *,
    details: Mapping[str, str] | None = None,
) -> str:
    labels = {
        "research": "required research is incomplete",
        "current_price": "no current complete market is available",
        "market_match": "market consensus or settlement verification is incomplete",
        "material_information": "material information is incomplete",
        "conservative_probability": "the conservative probability is invalid",
        "required_edge": "conservative edge is below 5.00 percentage points",
        "positive_ev": "expected value is not positive",
        "acceptable_price": "the offered price is below the minimum acceptable price",
        "odds_cutoff": "price falls outside -300 through +1000",
        "pick_strength": "pick-strength score is below the applicable confidence threshold",
        "conflict_and_integrity": "research conflict or data-integrity issue remains",
        "exposure": "an exposure limit blocks the selection",
    }
    detail_map = details or {}
    failed = [
        detail_map.get(name) or labels[name]
        for name in CORE_GATES
        if gates.get(name) is not True
    ]
    return "Pass — failed gates: " + "; ".join(failed or ["no qualifying candidate market"])


def _edge_score(edge: float) -> int:
    points = edge * 100
    if points < 2.0:
        return 0
    if points < 3.0:
        return 6
    if points < 4.0:
        return 8
    if points < 5.0:
        return 10
    if points < 6.0:
        return 12
    if points < 7.0:
        return 14
    if points < 8.0:
        return 16
    if points < 10.0:
        return 18
    return 20


def _ev_score(ev: float) -> int:
    percent = ev * 100
    if percent <= 0:
        return 0
    if percent < 5:
        return 2
    if percent < 10:
        return 5
    if percent < 15:
        return 7
    if percent < 25:
        return 9
    return 10


def _validation_score(connection: sqlite3.Connection, sport: str) -> int:
    count = validation_count(connection, sport)
    row = connection.execute(
        "SELECT * FROM multisport_validation_metrics WHERE validation_bucket=?",
        (validation_bucket(sport),),
    ).fetchone()
    if count < 100:
        return 10 if count > 0 else 0
    if row is None or row["calibration_status"] != "acceptable":
        return 0
    if count >= 500 and row["clv_status"] in {"stable", "favorable"}:
        return 25
    if count >= 250 and row["clv_status"] == "favorable":
        return 20
    return 15


def _market_quality_score(book_count: int) -> int:
    if book_count >= 3:
        return 15
    if book_count == 2:
        return 10
    if book_count == 1:
        return 5
    return 0


def _confidence(score: int | None, *, test_only: bool, all_core_gates: bool) -> str:
    if score is None or not all_core_gates or test_only or score < 75:
        return "Test-Only or Pass"
    if score >= 85:
        return "High Confidence"
    return "Medium Confidence"


def _spread_expected_points(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    prediction: sqlite3.Row,
) -> float | None:
    artifact = connection.execute(
        "SELECT feature_version FROM multisport_model_artifacts WHERE model_version=?",
        (prediction["model_version"],),
    ).fetchone()
    if artifact is None:
        return None
    features = feature_vector(
        connection,
        event_id=event_id,
        cutoff_time=_dt(str(prediction["feature_cutoff"])),
        feature_version=str(artifact["feature_version"]),
    )
    value = features.get("market_spread_points")
    return float(value) if value is not None else None


def _matching_views_for_candidate(
    views: tuple[MarketView, ...],
    *,
    sport: str,
    role: str,
    points: float | None,
    consensus_books: set[str],
) -> list[MarketView]:
    matched: list[MarketView] = []
    for view in views:
        if consensus_books and view.sportsbook_key not in consensus_books:
            continue
        if get_sport_market(sport).provider_market_key == "spreads":
            role_row = next(row for row in view.rows if row["outcome_role"] == role)
            if points is None or abs(float(role_row["points"]) - points) > 1e-9:
                continue
        matched.append(view)
    return matched


def evaluate_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    prediction_id: str,
    as_of: datetime,
    accessible_bookmakers: tuple[str, ...],
    consensus_bookmakers: tuple[str, ...],
    stale_after_minutes: int = 60,
    rule_version: str = RULE_VERSION,
    capture_unqualified_test: bool = False,
) -> EvaluationResult:
    """Evaluate every outcome and store exactly one final paper decision for an event."""
    ensure_runtime_schema(connection)
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (event_id,)).fetchone()
    prediction = connection.execute(
        "SELECT * FROM multisport_predictions WHERE prediction_id=? AND event_id=?",
        (prediction_id, event_id),
    ).fetchone()
    if event is None or prediction is None:
        raise ValueError("Event or prediction was not found")
    sport = str(event["sport"])
    spec = get_sport_market(sport)
    if prediction["market_key"] != spec.internal_market_key or prediction["period"] != spec.period:
        raise ValueError("Prediction does not match the controlled market")
    if _dt(str(prediction["prediction_time"])) > as_of.astimezone(UTC):
        raise ValueError("Evaluation cannot precede prediction time")
    if as_of.astimezone(UTC) >= _dt(str(event["commence_time"])):
        raise ValueError("Paper decision must be finalized before event start")

    probabilities = {k: float(v) for k, v in json.loads(prediction["probabilities_json"]).items()}
    uncertainty = float(prediction["uncertainty_margin"])
    engineering_baseline = _engineering_baseline_model(
        connection, str(prediction["model_version"])
    )
    countable_oos = 0 if engineering_baseline else 1
    push_probability = float(probabilities.get("push", 0.0))
    if not 0 <= uncertainty <= 1 or any(not 0 <= value <= 1 for value in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 1e-6:
        raise ValueError("Invalid probability distribution or uncertainty")
    research_ready, research_blockers = research_gate(
        connection,
        event_id=event_id,
        sport=sport,
        as_of=as_of,
        odds_stale_after_minutes=stale_after_minutes,
    )
    research_statuses = _latest_research_statuses(
        connection, event_id=event_id, sport=sport, as_of=as_of
    )
    conditional_research = bool(research_blockers) and all(
        status in {"verified", "conditional"} for status in research_statuses.values()
    ) and sum(1 for status in research_statuses.values() if status == "conditional") == 1

    views = current_market_views(
        connection,
        event_id=event_id,
        sport=sport,
        as_of=as_of,
        stale_after_minutes=stale_after_minutes,
    )
    accessible = set(accessible_bookmakers)
    consensus = set(consensus_bookmakers)
    expected_home_points = _spread_expected_points(connection, event_id=event_id, prediction=prediction)
    model_approved = approval_status(
        connection, sport, str(prediction["model_version"])
    ) == "approved"
    validation_total = validation_count(connection, sport)
    development = validation_total < spec.validation_target
    required_edge = STRICT_REQUIRED_EDGE
    minimum_score = MINIMUM_PICK_STRENGTH
    effective_uncertainty = uncertainty
    eligible_books = accessible
    core_gate_names = tuple(
        name for name in CORE_GATES if name != "pick_strength"
    )

    candidates: list[dict[str, object]] = []
    for view in views:
        if view.sportsbook_key not in eligible_books:
            continue
        for row in view.rows:
            role = str(row["outcome_role"])
            if role == "draw" and sport != "epl":
                continue
            points = float(row["points"]) if row["points"] is not None else None
            if spec.provider_market_key == "spreads":
                if expected_home_points is None:
                    continue
                expected = expected_home_points if role == "home" else -expected_home_points
                if points is None or abs(points - expected) > 1e-9:
                    continue
            matching_views = _matching_views_for_candidate(
                views,
                sport=sport,
                role=role,
                points=points,
                consensus_books=consensus,
            )
            consensus_probs = [market.probabilities[role] for market in matching_views]
            market_consensus = median(consensus_probs) if consensus_probs else None
            offered = Decimal(str(row["decimal_odds"]))
            raw_model = float(probabilities[role])
            if push_probability > 0:
                non_push = 1.0 - push_probability
                conditional_model = raw_model / non_push if non_push > 0 else 0.0
                conservative = max(0.0, conditional_model - effective_uncertainty)
                p_win = conservative * non_push
                p_loss = (1.0 - conservative) * non_push
                ev = float(push_aware_ev(p_win, p_loss, push_probability, offered))
                model_for_edge = conditional_model
            else:
                model_for_edge = raw_model
                conservative = max(0.0, raw_model - effective_uncertainty)
                ev = float(standard_ev(conservative, offered))
            break_even = float(break_even_probability(offered))
            edge = conservative - break_even
            try:
                minimum_price = float(minimum_acceptable_decimal_odds(conservative, required_edge))
            except ValueError:
                minimum_price = float("inf")
            settlement_ok = _settlement_verified(
                connection,
                sportsbook_key=view.sportsbook_key,
                sport=sport,
            )
            info_score = 20 if research_ready else (10 if conditional_research else 0)
            stability_score = 10 if edge - 0.01 >= float(required_edge) else (5 if edge >= float(required_edge) else 0)
            score = (
                _edge_score(edge)
                + _ev_score(ev)
                + _validation_score(connection, sport)
                + info_score
                + _market_quality_score(len(matching_views))
                + stability_score
            )
            outcome_participant_id = str(row["outcome_participant_id"])
            exposure = assess_exposure(
                connection,
                event_id=event_id,
                sport=sport,
                outcome_participant_id=outcome_participant_id,
                as_of=as_of,
            )
            gates = {
                "research": research_ready,
                "current_price": True,
                "market_match": settlement_ok and (market_consensus is not None),
                "material_information": research_ready,
                "model_eligibility": model_approved,
                "conservative_probability": effective_uncertainty >= 0,
                "required_edge": edge >= float(required_edge),
                "positive_ev": ev > 0,
                "acceptable_price": float(offered) >= minimum_price,
                "odds_cutoff": Decimal("1") + Decimal("100") / Decimal("300") <= offered <= Decimal("11"),
                "confluence": len(matching_views) >= 2,
                "pick_strength": score >= minimum_score,
                "conflict_and_integrity": not any(status == "conflict" for status in research_statuses.values()),
                "exposure": exposure.clear,
                "manual_approval": False,
            }
            core = all(gates[name] for name in core_gate_names)
            gate_details: dict[str, str] = {}
            if research_blockers:
                gate_details["research"] = "research incomplete: " + ", ".join(research_blockers)
                gate_details["material_information"] = gate_details["research"]
            if market_consensus is None:
                gate_details["market_match"] = "no current matching consensus market"
            elif not settlement_ok:
                gate_details["market_match"] = (
                    f"settlement rule is not verified for {view.sportsbook_key}"
                )
            if edge < float(required_edge):
                gate_details["required_edge"] = (
                    f"conservative edge is {edge * 100:.2f} points; {float(required_edge) * 100:.2f} required"
                )
            if ev <= 0:
                gate_details["positive_ev"] = f"expected value is {ev * 100:.2f}%"
            if float(offered) < minimum_price:
                gate_details["acceptable_price"] = (
                    f"offered decimal odds {float(offered):.3f} are below "
                    f"the {minimum_price:.3f} minimum"
                )
            if offered <= MINIMUM_ELIGIBLE_DECIMAL_ODDS:
                gate_details["odds_cutoff"] = "Price outside the amended -300 to +1000 band"
            if score < minimum_score:
                gate_details["pick_strength"] = (
                    f"pick-strength score is {score}/100; {minimum_score} required for Official research"
                )
            if exposure.blockers:
                gate_details["exposure"] = ", ".join(exposure.blockers)
            candidates.append(
                {
                    "row": row,
                    "role": role,
                    "points": points,
                    "offered": float(offered),
                    "break_even": break_even,
                    "market_consensus": market_consensus,
                    "model_probability": model_for_edge,
                    "conservative": conservative,
                    "edge": edge,
                    "ev": ev,
                    "minimum_price": minimum_price,
                    "effective_uncertainty": effective_uncertainty,
                    "score": score,
                    "gates": gates,
                    "gate_details": gate_details,
                    "core": core,
                    "matching_book_count": len(matching_views),
                    "settlement_ok": settlement_ok,
                }
            )

    eligible = [candidate for candidate in candidates if candidate["core"]]
    eligible.sort(
        key=lambda item: (
            int(item["score"]), float(item["ev"]), float(item["edge"]),
            int(item["matching_book_count"]), float(item["offered"]),
        ),
        reverse=True,
    )
    selected = eligible[0] if eligible else None
    unqualified_test_observation = False
    if selected is None and capture_unqualified_test and candidates:
        selected = max(
            candidates,
            key=lambda item: (
                float(item["model_probability"]),
                float(item["offered"]),
                int(item["matching_book_count"]),
                str(item["role"]),
            ),
        )
        decision_type = "test_only_selection"
        pass_reason = _failed_gate_reason(
            selected["gates"], details=selected["gate_details"]
        )
        countable_oos = 0
        unqualified_test_observation = True
    elif selected is None:
        if conditional_research and candidates:
            conditional_candidates = [
                candidate
                for candidate in candidates
                if all(
                    candidate["gates"][name]
                    for name in core_gate_names
                    if name not in {"research", "material_information"}
                )
            ]
            best = max(
                conditional_candidates,
                key=lambda item: (int(item["score"]), float(item["ev"]), float(item["edge"])),
            ) if conditional_candidates else None
            decision_type = "conditional_pick" if best is not None else "pass"
            selected = best if decision_type == "conditional_pick" else None
            if selected is not None:
                pass_reason = None
            else:
                diagnostic = max(
                    candidates,
                    key=lambda item: (int(item["score"]), float(item["ev"]), float(item["edge"])),
                )
                pass_reason = _failed_gate_reason(
                    diagnostic["gates"], details=diagnostic["gate_details"]
                )
        else:
            decision_type = "pass"
            if candidates:
                diagnostic = max(
                    candidates,
                    key=lambda item: (int(item["score"]), float(item["ev"]), float(item["edge"])),
                )
                pass_reason = _failed_gate_reason(
                    diagnostic["gates"], details=diagnostic["gate_details"]
                )
            else:
                empty_gates = {
                    "research": research_ready,
                    "current_price": bool(views),
                    "market_match": False,
                    "material_information": research_ready,
                    "conservative_probability": uncertainty >= 0,
                    "required_edge": False,
                    "positive_ev": False,
                    "acceptable_price": False,
                    "odds_cutoff": False,
                    "pick_strength": False,
                    "conflict_and_integrity": not any(
                        status == "conflict" for status in research_statuses.values()
                    ),
                    "exposure": True,
                }
                empty_details = {}
                if research_blockers:
                    empty_details["research"] = (
                        "research incomplete: " + ", ".join(research_blockers)
                    )
                    empty_details["material_information"] = empty_details["research"]
                pass_reason = _failed_gate_reason(empty_gates, details=empty_details)
    elif int(selected["matching_book_count"]) < 4:
        decision_type = "conditional_pick"
        pass_reason = None
    elif not model_approved or validation_total < spec.validation_target or int(selected["score"]) < 75 or engineering_baseline:
        decision_type = "test_only_selection"
        pass_reason = None
    else:
        decision_type = "official_pick" if int(selected["matching_book_count"]) >= 4 else "conditional_pick"
        pass_reason = None

    decision_id = f"msd-{uuid4().hex}"
    if selected is not None:
        row = selected["row"]
        score = int(selected["score"])
        all_core = bool(selected["core"])
        if unqualified_test_observation:
            confidence = "Unqualified Test Observation"
        elif engineering_baseline or not model_approved or validation_total < spec.validation_target:
            confidence = "experimental (unvalidated model)"
        elif decision_type == "official_pick":
            confidence = "High - strong edge" if score >= 85 else "Medium - strong edge"
        elif int(selected["matching_book_count"]) < 4:
            confidence = "Conditional - confluence shortfall"
        else:
            confidence = "Experimental - thin edge"
        gates = dict(selected["gates"])
        reasons = [
            f"Conservative edge {float(selected['edge']) * 100:.2f} percentage points",
            f"EV {float(selected['ev']) * 100:.2f}% per dollar risked",
            f"Median market consensus from {int(selected['matching_book_count'])} matching book(s)",
            f"Pick strength {score}/100 ({letter_grade(score)})",
            f"Validation sample {validation_total}/{spec.validation_target}",
        ]
        if engineering_baseline:
            reasons.append(
                "Engineering market baseline only; this decision does not count toward the 100 OOS target"
            )
        if unqualified_test_observation:
            reasons.extend(
                [
                    "Captured for testing despite failed qualification gates at the owner's direction.",
                    "This observation is non-countable: it does not increase training examples or OOS validation.",
                    pass_reason,
                ]
            )
        loss_reasons = [
            "The model probability can be wrong even when the process is correct.",
            "Late information or market movement can remove the measured edge.",
        ]
        connection.execute(
            """
            INSERT INTO multisport_paper_decisions (
                paper_decision_id, event_id, prediction_id, sport, market_key, period,
                outcome_role, outcome_participant_id, sportsbook_key, odds_snapshot_id,
                points, decimal_odds, check_time, break_even_probability,
                market_consensus_probability, model_probability, uncertainty_margin,
                conservative_probability, probability_edge, ev_per_dollar,
                minimum_acceptable_decimal_odds, pick_strength_score, confidence_label,
                decision_type, gate_results_json, reasons_json, loss_reasons_json,
                pass_reason, manual_approval_status, countable_oos, model_version,
                rule_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                decision_id, event_id, prediction_id, sport, spec.internal_market_key, spec.period,
                selected["role"], row["outcome_participant_id"], row["sportsbook_key"],
                row["odds_snapshot_id"], selected["points"], selected["offered"], _iso(as_of),
                selected["break_even"], selected["market_consensus"], selected["model_probability"],
                selected["effective_uncertainty"], selected["conservative"], selected["edge"], selected["ev"],
                selected["minimum_price"], score, confidence, decision_type,
                json.dumps(gates, sort_keys=True), json.dumps(reasons), json.dumps(loss_reasons),
                pass_reason, countable_oos, prediction["model_version"], rule_version, _iso(as_of),
            ),
        )
        result = EvaluationResult(
            paper_decision_id=decision_id,
            decision_type=decision_type,
            outcome_role=str(selected["role"]),
            sportsbook_key=str(row["sportsbook_key"]),
            decimal_odds=float(selected["offered"]),
            probability_edge=float(selected["edge"]),
            ev_per_dollar=float(selected["ev"]),
            pick_strength_score=score,
            letter_grade=letter_grade(score),
            paper_units=PAPER_UNITS_PER_SELECTION,
            paper_unit_dollars=DEFAULT_PAPER_UNIT_DOLLARS,
            paper_risk_dollars=DEFAULT_PAPER_UNIT_DOLLARS * PAPER_UNITS_PER_SELECTION,
            profit_if_win_dollars=round(
                DEFAULT_PAPER_UNIT_DOLLARS
                * PAPER_UNITS_PER_SELECTION
                * (float(selected["offered"]) - 1.0),
                2,
            ),
            total_return_if_win_dollars=round(
                DEFAULT_PAPER_UNIT_DOLLARS
                * PAPER_UNITS_PER_SELECTION
                * float(selected["offered"]),
                2,
            ),
            confidence_label=confidence,
            pass_reason=pass_reason,
        )
    else:
        if candidates:
            diagnostic = max(
                candidates,
                key=lambda item: (int(item["score"]), float(item["ev"]), float(item["edge"])),
            )
            gates = dict(diagnostic["gates"])
        else:
            gates = {
                "research": research_ready,
                "current_price": bool(views),
                "market_match": False,
                "material_information": research_ready,
                "model_eligibility": model_approved,
                "conservative_probability": uncertainty >= 0,
                "required_edge": False,
                "positive_ev": False,
                "acceptable_price": False,
                "pick_strength": False,
                "conflict_and_integrity": not any(
                    status == "conflict" for status in research_statuses.values()
                ),
                "exposure": True,
                "manual_approval": False,
            }
        connection.execute(
            """
            INSERT INTO multisport_paper_decisions (
                paper_decision_id, event_id, prediction_id, sport, market_key, period,
                check_time, decision_type, gate_results_json, pass_reason,
                manual_approval_status, countable_oos, model_version, rule_version, created_at,
                reasons_json, loss_reasons_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pass', ?, ?, 'pending', ?, ?, ?, ?, '[]', '[]')
            """,
            (
                decision_id, event_id, prediction_id, sport, spec.internal_market_key, spec.period,
                _iso(as_of), json.dumps(gates, sort_keys=True), pass_reason,
                countable_oos, prediction["model_version"], rule_version, _iso(as_of),
            ),
        )
        result = EvaluationResult(
            paper_decision_id=decision_id,
            decision_type="pass",
            outcome_role=None,
            sportsbook_key=None,
            decimal_odds=None,
            probability_edge=None,
            ev_per_dollar=None,
            pick_strength_score=None,
            letter_grade=None,
            paper_units=0.0,
            paper_unit_dollars=DEFAULT_PAPER_UNIT_DOLLARS,
            paper_risk_dollars=0.0,
            profit_if_win_dollars=None,
            total_return_if_win_dollars=None,
            confidence_label="Test-Only or Pass",
            pass_reason=pass_reason,
        )
    connection.commit()
    return result


def approve_for_manual_wager(
    connection: sqlite3.Connection,
    *,
    paper_decision_id: str,
) -> bool:
    """Manual approval gate. This never takes a real-money action."""
    ensure_runtime_schema(connection)
    row = connection.execute(
        "SELECT * FROM multisport_paper_decisions WHERE paper_decision_id=?",
        (paper_decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown paper decision")
    gates = json.loads(row["gate_results_json"])
    sport = str(row["sport"])
    spec = get_sport_market(sport)
    approved_model = approval_status(connection, sport, str(row["model_version"])) == "approved"
    eligible = (
        row["decision_type"] in {"conditional_pick", "official_pick"}
        and approved_model
        and validation_count(connection, sport) >= spec.validation_target
        and all(
            gates.get(name) is True
            for name in (
                "research", "current_price", "market_match", "material_information",
                "conservative_probability", "required_edge", "positive_ev",
                "acceptable_price", "pick_strength", "conflict_and_integrity", "exposure",
            )
        )
    )
    if not eligible:
        connection.execute(
            "UPDATE multisport_paper_decisions SET manual_approval_status='rejected' WHERE paper_decision_id=?",
            (paper_decision_id,),
        )
        connection.commit()
        return False
    gates["manual_approval"] = True
    gates["model_eligibility"] = True
    connection.execute(
        """
        UPDATE multisport_paper_decisions
        SET manual_approval_status='approved_for_manual_wager',
            decision_type='official_pick', gate_results_json=?
        WHERE paper_decision_id=?
        """,
        (json.dumps(gates, sort_keys=True), paper_decision_id),
    )
    connection.commit()
    return True


def ranked_decisions(
    connection: sqlite3.Connection,
    *,
    created_on_or_after: datetime | None = None,
    include_test_only: bool = True,
) -> list[sqlite3.Row]:
    ensure_runtime_schema(connection)
    decision_types = ("official_pick", "conditional_pick", "test_only_selection") if include_test_only else ("official_pick",)
    placeholders = ",".join("?" for _ in decision_types)
    params: list[object] = list(decision_types)
    where = f"decision_type IN ({placeholders})"
    if created_on_or_after is not None:
        where += " AND check_time>=?"
        params.append(_iso(created_on_or_after))
    return connection.execute(
        f"""
        SELECT * FROM multisport_paper_decisions
        WHERE {where}
        ORDER BY pick_strength_score DESC, ev_per_dollar DESC,
                 probability_edge DESC, uncertainty_margin ASC, check_time DESC
        """,
        tuple(params),
    ).fetchall()


def best_single(connection: sqlite3.Connection, *, official_only: bool = False) -> sqlite3.Row | None:
    rows = ranked_decisions(connection, include_test_only=not official_only)
    return rows[0] if rows else None


def build_three_leg_parlay(
    connection: sqlite3.Connection,
    *,
    paper_decision_ids: tuple[str, str, str],
    offered_decimal_odds: float | None,
    measured_joint_probability: float | None,
    correlation_method: str | None,
    created_at: datetime,
) -> str:
    """Record a three-leg parlay only when correlation has been measured.

    The project rules prohibit multiplying legs when important correlation is unknown.
    Therefore missing joint probability or correlation evidence creates a pass record.
    """
    ensure_runtime_schema(connection)
    placeholders = ",".join("?" for _ in paper_decision_ids)
    rows = connection.execute(
        f"SELECT * FROM multisport_paper_decisions WHERE paper_decision_id IN ({placeholders})",
        paper_decision_ids,
    ).fetchall()
    if len(rows) != 3:
        raise ValueError("Exactly three valid decision IDs are required")
    legs = [dict(row) for row in rows]
    pass_reason = None
    decision_type = "test_only_selection"
    ev = None
    if any(row["decision_type"] == "pass" for row in rows):
        pass_reason = "A parlay cannot include a pass leg."
    elif measured_joint_probability is None or not correlation_method:
        pass_reason = "Important parlay correlation has not been measured."
    elif not 0 < measured_joint_probability < 1:
        raise ValueError("Joint probability must be between zero and one")
    elif offered_decimal_odds is None or offered_decimal_odds <= 1:
        pass_reason = "Current offered parlay odds are unavailable."
    else:
        ev = measured_joint_probability * offered_decimal_odds - 1.0
        if ev <= 0:
            pass_reason = "Pass - no reliable edge."
    if pass_reason:
        decision_type = "pass"
    parlay_id = f"msp-{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO multisport_parlays (
            parlay_id, created_at, leg_count, decimal_odds, joint_probability,
            ev_per_dollar, correlation_method, decision_type, pass_reason,
            manual_approval_status, legs_json
        ) VALUES (?, ?, 3, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            parlay_id, _iso(created_at), offered_decimal_odds, measured_joint_probability,
            ev, correlation_method or "unmeasured", decision_type, pass_reason,
            json.dumps([
                {
                    "paper_decision_id": row["paper_decision_id"],
                    "sport": row["sport"],
                    "event_id": row["event_id"],
                    "outcome_role": row["outcome_role"],
                    "model_probability": row["model_probability"],
                }
                for row in rows
            ], sort_keys=True),
        ),
    )
    connection.commit()
    return parlay_id

