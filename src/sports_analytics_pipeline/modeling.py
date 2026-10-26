"""Phase 4.1: an interpretable, market-benchmarked NFL spread model.

The model is intentionally small and auditable:

* pregame Elo ratings are calculated only from earlier final games;
* 2020-2023 regular seasons fit expected scoring margin;
* 2024 calibrates cover probabilities;
* 2025 is a historical audit period;
* candidate probabilities must beat both a 50% baseline and the sportsbook's
  closing no-vig baseline on the same audited decisions;
* current exact lines and timestamped prices are used only after training.

Preseason predictions remain test-only.  A fixed preseason uncertainty guard is
deducted because regular-season playing time does not transfer cleanly to
preseason.  Missing history, a failed historical audit, stale odds, or an unsupported
whole-number push rate results in a pass rather than a fabricated number.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from typing import Any, Iterable

from .calculations import remove_vig


MODEL_VERSION = "our-market-benchmarked-spread-v1.1"
FEATURE_VERSION = "pregame-elo-closing-market-audit-v1.1"
RULE_VERSION = "our-first-model-v1.1"
DEFAULT_WINDOW_HOURS = 24
DEVELOPMENT_SEASONS = (2020, 2021, 2022, 2023)
VALIDATION_SEASON = 2024
LOCKED_TEST_SEASON = 2025
MINIMUM_TRAINING_GAMES = 800
MINIMUM_VALIDATION_GAMES = 200
MINIMUM_LOCKED_TEST_GAMES = 200
PRESEASON_UNCERTAINTY_GUARD = 0.05
REGULAR_UNCERTAINTY_FLOOR = 0.01


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Model timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Stored timestamp is missing a time zone")
    return parsed.astimezone(timezone.utc)


def _clamp(value: float, low: float = 1e-6, high: float = 1 - 1e-6) -> float:
    return max(low, min(high, value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        factor = math.exp(-value)
        return 1 / (1 + factor)
    factor = math.exp(value)
    return factor / (1 + factor)


def _logit(probability: float) -> float:
    value = _clamp(probability)
    return math.log(value / (1 - value))


def _normal_cdf(value: float) -> float:
    return 0.5 * (1 + math.erf(value / math.sqrt(2)))


@dataclass(frozen=True)
class HistoricalExample:
    source_game_id: str
    season: int
    kickoff_time: datetime
    home_team_id: str
    away_team_id: str
    rating_difference: float
    home_spread_points: float
    scoring_margin: float
    home_market_no_vig_probability: float | None

    @property
    def cover_result(self) -> float:
        return self.scoring_margin + self.home_spread_points


@dataclass(frozen=True)
class ModelArtifact:
    model_version: str
    feature_version: str
    rule_version: str
    trained_at: datetime
    training_cutoff: datetime
    intercept: float
    elo_coefficient: float
    residual_standard_deviation: float
    calibration_intercept: float
    calibration_slope: float
    validation_ece: float
    validation_brier: float
    validation_log_loss: float
    locked_test_ece: float
    locked_test_brier: float
    locked_test_log_loss: float
    baseline_brier: float
    baseline_log_loss: float
    market_baseline_brier: float
    market_baseline_log_loss: float
    market_baseline_coverage: float
    training_count: int
    validation_count: int
    locked_test_count: int
    push_rates: dict[str, float]
    data_hash: str
    status: str

    def probability(self, rating_difference: float, home_spread_points: float) -> float:
        expected_margin = self.intercept + self.elo_coefficient * (
            rating_difference / 400.0
        )
        raw = _normal_cdf(
            (expected_margin + home_spread_points)
            / self.residual_standard_deviation
        )
        return _sigmoid(
            self.calibration_intercept
            + self.calibration_slope * _logit(raw)
        )

    def push_rate(self, home_spread_points: float) -> float | None:
        if not float(home_spread_points).is_integer():
            return 0.0
        exact = f"{abs(int(home_spread_points))}"
        if exact in self.push_rates:
            return self.push_rates[exact]
        return self.push_rates.get("all_whole")


def _historical_examples(
    db: sqlite3.Connection,
) -> tuple[list[HistoricalExample], dict[str, float]]:
    rows = db.execute(
        """
        SELECT source_game_id, season, kickoff_time, home_team_id, away_team_id,
               home_score, away_score, home_spread_points,
               home_market_no_vig_probability
        FROM historical_training_games
        ORDER BY kickoff_time, source_game_id
        """
    ).fetchall()
    ratings: dict[str, float] = {}
    examples: list[HistoricalExample] = []
    for row in rows:
        home = str(row["home_team_id"])
        away = str(row["away_team_id"])
        home_rating = ratings.get(home, 1500.0)
        away_rating = ratings.get(away, 1500.0)
        difference = home_rating - away_rating
        margin = float(row["home_score"] - row["away_score"])
        examples.append(
            HistoricalExample(
                source_game_id=str(row["source_game_id"]),
                season=int(row["season"]),
                kickoff_time=_parse(str(row["kickoff_time"])),
                home_team_id=home,
                away_team_id=away,
                rating_difference=difference,
                home_spread_points=float(row["home_spread_points"]),
                scoring_margin=margin,
                home_market_no_vig_probability=(
                    None
                    if row["home_market_no_vig_probability"] is None
                    else float(row["home_market_no_vig_probability"])
                ),
            )
        )

        expected_home = _sigmoid(
            math.log(10) * ((home_rating + 35.0) - away_rating) / 400.0
        )
        actual_home = 1.0 if margin > 0 else 0.0 if margin < 0 else 0.5
        change = 20.0 * (actual_home - expected_home)
        ratings[home] = home_rating + change
        ratings[away] = away_rating - change
    return examples, ratings


def _fit_margin_model(examples: list[HistoricalExample]) -> tuple[float, float, float]:
    if len(examples) < 3:
        raise ValueError("At least three development games are required")
    x = [item.rating_difference / 400.0 for item in examples]
    y = [item.scoring_margin for item in examples]
    mean_x = statistics.fmean(x)
    mean_y = statistics.fmean(y)
    denominator = sum((value - mean_x) ** 2 for value in x)
    if denominator <= 0:
        raise ValueError("Development ratings have no usable variation")
    coefficient = sum(
        (left - mean_x) * (right - mean_y) for left, right in zip(x, y)
    ) / denominator
    intercept = mean_y - coefficient * mean_x
    residuals = [
        outcome - (intercept + coefficient * feature)
        for feature, outcome in zip(x, y)
    ]
    residual_sd = math.sqrt(
        sum(value * value for value in residuals) / (len(residuals) - 2)
    )
    if residual_sd <= 0:
        raise ValueError("Development residual variation is not positive")
    return intercept, coefficient, residual_sd


def _raw_probability(
    item: HistoricalExample,
    *,
    intercept: float,
    coefficient: float,
    residual_sd: float,
) -> float:
    expected_margin = intercept + coefficient * (item.rating_difference / 400.0)
    return _clamp(
        _normal_cdf((expected_margin + item.home_spread_points) / residual_sd)
    )


def _fit_platt(probabilities: list[float], outcomes: list[int]) -> tuple[float, float]:
    if len(probabilities) != len(outcomes) or not probabilities:
        raise ValueError("Calibration probabilities and outcomes must align")
    intercept = 0.0
    slope = 1.0
    regularization = 1e-4
    for _ in range(60):
        g0 = 0.0
        g1 = -regularization * slope
        h00 = 0.0
        h01 = 0.0
        h11 = regularization
        for probability, outcome in zip(probabilities, outcomes):
            feature = _logit(probability)
            fitted = _sigmoid(intercept + slope * feature)
            error = outcome - fitted
            weight = max(fitted * (1 - fitted), 1e-9)
            g0 += error
            g1 += error * feature
            h00 += weight
            h01 += weight * feature
            h11 += weight * feature * feature
        determinant = h00 * h11 - h01 * h01
        if determinant <= 1e-12:
            break
        delta0 = (g0 * h11 - g1 * h01) / determinant
        delta1 = (g1 * h00 - g0 * h01) / determinant
        intercept += delta0
        slope += delta1
        if max(abs(delta0), abs(delta1)) < 1e-8:
            break
    return intercept, max(0.0, slope)


def _brier(probabilities: Iterable[float], outcomes: Iterable[int]) -> float:
    pairs = list(zip(probabilities, outcomes))
    return statistics.fmean((probability - outcome) ** 2 for probability, outcome in pairs)


def _log_loss(probabilities: Iterable[float], outcomes: Iterable[int]) -> float:
    pairs = list(zip(probabilities, outcomes))
    return statistics.fmean(
        -(outcome * math.log(_clamp(probability))
          + (1 - outcome) * math.log(_clamp(1 - probability)))
        for probability, outcome in pairs
    )


def _ece(probabilities: list[float], outcomes: list[int], bins: int = 10) -> float:
    groups: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, outcome in zip(probabilities, outcomes):
        index = min(bins - 1, int(probability * bins))
        groups[index].append((probability, outcome))
    total = len(probabilities)
    return sum(
        (len(group) / total)
        * abs(
            statistics.fmean(item[0] for item in group)
            - statistics.fmean(item[1] for item in group)
        )
        for group in groups
        if group
    )


def _non_push(examples: Iterable[HistoricalExample]) -> tuple[list[HistoricalExample], list[int]]:
    items: list[HistoricalExample] = []
    outcomes: list[int] = []
    for item in examples:
        result = item.cover_result
        if abs(result) < 1e-9:
            continue
        items.append(item)
        outcomes.append(1 if result > 0 else 0)
    return items, outcomes


def _push_rates(examples: Iterable[HistoricalExample]) -> dict[str, float]:
    exact: dict[str, list[bool]] = {}
    all_whole: list[bool] = []
    for item in examples:
        if not float(item.home_spread_points).is_integer():
            continue
        key = f"{abs(int(item.home_spread_points))}"
        pushed = abs(item.cover_result) < 1e-9
        exact.setdefault(key, []).append(pushed)
        all_whole.append(pushed)
    rates = {
        key: sum(values) / len(values)
        for key, values in exact.items()
        if len(values) >= 20
    }
    if len(all_whole) >= 100:
        rates["all_whole"] = sum(all_whole) / len(all_whole)
    return rates


def _market_comparison(
    examples: list[HistoricalExample],
    outcomes: list[int],
    model_probabilities: list[float],
) -> dict[str, float]:
    """Score the candidate and market on exactly the same available decisions."""
    paired = [
        (model, item.home_market_no_vig_probability, outcome)
        for item, outcome, model in zip(examples, outcomes, model_probabilities)
        if item.home_market_no_vig_probability is not None
    ]
    if not paired:
        return {
            "coverage": 0.0,
            "candidate_brier": 1.0,
            "candidate_log_loss": 10.0,
            "market_brier": 1.0,
            "market_log_loss": 10.0,
        }
    candidate = [item[0] for item in paired]
    market = [_clamp(float(item[1])) for item in paired]
    paired_outcomes = [item[2] for item in paired]
    return {
        "coverage": len(paired) / len(examples),
        "candidate_brier": _brier(candidate, paired_outcomes),
        "candidate_log_loss": _log_loss(candidate, paired_outcomes),
        "market_brier": _brier(market, paired_outcomes),
        "market_log_loss": _log_loss(market, paired_outcomes),
    }


def train_first_model(
    db: sqlite3.Connection,
    *,
    as_of: datetime,
    minimum_training_games: int = MINIMUM_TRAINING_GAMES,
    minimum_validation_games: int = MINIMUM_VALIDATION_GAMES,
    minimum_locked_test_games: int = MINIMUM_LOCKED_TEST_GAMES,
    persist: bool = True,
) -> tuple[ModelArtifact | None, dict[str, Any]]:
    as_of = as_of.astimezone(timezone.utc)
    examples, _ = _historical_examples(db)
    development = [item for item in examples if item.season in DEVELOPMENT_SEASONS]
    validation_all = [item for item in examples if item.season == VALIDATION_SEASON]
    locked_all = [item for item in examples if item.season == LOCKED_TEST_SEASON]
    validation, validation_outcomes = _non_push(validation_all)
    locked, locked_outcomes = _non_push(locked_all)
    readiness = {
        "development_games": len(development),
        "validation_decisions": len(validation),
        "locked_test_decisions": len(locked),
        "required": {
            "development_games": minimum_training_games,
            "validation_decisions": minimum_validation_games,
            "locked_test_decisions": minimum_locked_test_games,
        },
    }
    missing = [
        name
        for name, actual, required in (
            ("development_games", len(development), minimum_training_games),
            ("validation_decisions", len(validation), minimum_validation_games),
            ("locked_test_decisions", len(locked), minimum_locked_test_games),
        )
        if actual < required
    ]
    if missing:
        return None, {
            "status": "blocked",
            "reason": "Historical model data is incomplete.",
            "missing_requirements": missing,
            "readiness": readiness,
            "next_command": "./bot history",
        }

    intercept, coefficient, residual_sd = _fit_margin_model(development)
    validation_raw = [
        _raw_probability(
            item,
            intercept=intercept,
            coefficient=coefficient,
            residual_sd=residual_sd,
        )
        for item in validation
    ]
    calibration_intercept, calibration_slope = _fit_platt(
        validation_raw, validation_outcomes
    )
    validation_probabilities = [
        _sigmoid(calibration_intercept + calibration_slope * _logit(value))
        for value in validation_raw
    ]
    locked_raw = [
        _raw_probability(
            item,
            intercept=intercept,
            coefficient=coefficient,
            residual_sd=residual_sd,
        )
        for item in locked
    ]
    locked_probabilities = [
        _sigmoid(calibration_intercept + calibration_slope * _logit(value))
        for value in locked_raw
    ]
    validation_ece = _ece(validation_probabilities, validation_outcomes)
    validation_brier = _brier(validation_probabilities, validation_outcomes)
    validation_log_loss = _log_loss(validation_probabilities, validation_outcomes)
    locked_ece = _ece(locked_probabilities, locked_outcomes)
    locked_brier = _brier(locked_probabilities, locked_outcomes)
    locked_log_loss = _log_loss(locked_probabilities, locked_outcomes)
    baseline_brier = _brier([0.5] * len(locked_outcomes), locked_outcomes)
    baseline_log_loss = _log_loss([0.5] * len(locked_outcomes), locked_outcomes)
    validation_market = _market_comparison(
        validation, validation_outcomes, validation_probabilities
    )
    locked_market = _market_comparison(
        locked, locked_outcomes, locked_probabilities
    )
    acceptance_checks = {
        "validation_ece_at_most_0_08": validation_ece <= 0.08,
        "audit_beats_50pct_brier": locked_brier < baseline_brier,
        "audit_beats_50pct_log_loss": locked_log_loss < baseline_log_loss,
        "market_baseline_coverage_at_least_90pct": locked_market["coverage"] >= 0.90,
        "audit_beats_market_brier": (
            locked_market["candidate_brier"] < locked_market["market_brier"]
        ),
        "audit_beats_market_log_loss": (
            locked_market["candidate_log_loss"] < locked_market["market_log_loss"]
        ),
    }
    acceptable = (
        all(acceptance_checks.values())
    )
    status = "acceptable" if acceptable else "rejected"
    training_cutoff = max(item.kickoff_time for item in validation_all)
    hash_payload = [
        (
            item.source_game_id,
            item.season,
            item.kickoff_time.isoformat(),
            item.home_team_id,
            item.away_team_id,
            item.scoring_margin,
            item.home_spread_points,
        )
        for item in examples
    ]
    data_hash = hashlib.sha256(
        json.dumps(hash_payload, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact = ModelArtifact(
        model_version=MODEL_VERSION,
        feature_version=FEATURE_VERSION,
        rule_version=RULE_VERSION,
        trained_at=as_of,
        training_cutoff=training_cutoff,
        intercept=intercept,
        elo_coefficient=coefficient,
        residual_standard_deviation=residual_sd,
        calibration_intercept=calibration_intercept,
        calibration_slope=calibration_slope,
        validation_ece=validation_ece,
        validation_brier=validation_brier,
        validation_log_loss=validation_log_loss,
        locked_test_ece=locked_ece,
        locked_test_brier=locked_brier,
        locked_test_log_loss=locked_log_loss,
        baseline_brier=baseline_brier,
        baseline_log_loss=baseline_log_loss,
        market_baseline_brier=locked_market["market_brier"],
        market_baseline_log_loss=locked_market["market_log_loss"],
        market_baseline_coverage=locked_market["coverage"],
        training_count=len(development),
        validation_count=len(validation),
        locked_test_count=len(locked),
        push_rates=_push_rates(development + validation_all),
        data_hash=data_hash,
        status=status,
    )
    if persist:
        coefficients = {
            "intercept": intercept,
            "elo_coefficient": coefficient,
            "residual_standard_deviation": residual_sd,
        }
        calibration = {
            "intercept": calibration_intercept,
            "slope": calibration_slope,
        }
        metrics = {
            "validation_ece": validation_ece,
            "validation_brier": validation_brier,
            "validation_log_loss": validation_log_loss,
            "validation_market_baseline_brier": validation_market["market_brier"],
            "validation_market_baseline_log_loss": validation_market["market_log_loss"],
            "validation_market_baseline_coverage": validation_market["coverage"],
            "locked_test_ece": locked_ece,
            "locked_test_brier": locked_brier,
            "locked_test_log_loss": locked_log_loss,
            "baseline_brier": baseline_brier,
            "baseline_log_loss": baseline_log_loss,
            "market_baseline_brier": locked_market["market_brier"],
            "market_baseline_log_loss": locked_market["market_log_loss"],
            "market_baseline_coverage": locked_market["coverage"],
            "candidate_brier_on_market_rows": locked_market["candidate_brier"],
            "candidate_log_loss_on_market_rows": locked_market["candidate_log_loss"],
            "acceptance_checks": acceptance_checks,
        }
        with db:
            db.execute(
                """
                INSERT INTO model_artifacts (
                    model_version, feature_version, rule_version, trained_at,
                    training_cutoff, training_count, validation_count,
                    locked_test_count, coefficients_json, calibration_json,
                    push_rates_json, metrics_json, data_hash, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(model_version) DO UPDATE SET
                    feature_version = excluded.feature_version,
                    rule_version = excluded.rule_version,
                    trained_at = excluded.trained_at,
                    training_cutoff = excluded.training_cutoff,
                    training_count = excluded.training_count,
                    validation_count = excluded.validation_count,
                    locked_test_count = excluded.locked_test_count,
                    coefficients_json = excluded.coefficients_json,
                    calibration_json = excluded.calibration_json,
                    push_rates_json = excluded.push_rates_json,
                    metrics_json = excluded.metrics_json,
                    data_hash = excluded.data_hash,
                    status = excluded.status
                """,
                (
                    artifact.model_version,
                    artifact.feature_version,
                    artifact.rule_version,
                    _iso(artifact.trained_at),
                    _iso(artifact.training_cutoff),
                    artifact.training_count,
                    artifact.validation_count,
                    artifact.locked_test_count,
                    json.dumps(coefficients, sort_keys=True),
                    json.dumps(calibration, sort_keys=True),
                    json.dumps(artifact.push_rates, sort_keys=True),
                    json.dumps(metrics, sort_keys=True),
                    artifact.data_hash,
                    artifact.status,
                ),
            )
    return artifact, {
        "status": status,
        "readiness": readiness,
        "model_version": artifact.model_version,
        "feature_version": artifact.feature_version,
        "time_split": {
            "development": "2020-2023 regular season",
            "validation": "2024 regular season",
            "historical_audit": "2025 regular season",
            "next_untouched_period": "2026 regular season live paper decisions",
        },
        "metrics": {
            "validation_ece": round(validation_ece, 6),
            "validation_brier": round(validation_brier, 6),
            "validation_log_loss": round(validation_log_loss, 6),
            "locked_test_ece": round(locked_ece, 6),
            "locked_test_brier": round(locked_brier, 6),
            "baseline_brier": round(baseline_brier, 6),
            "locked_test_log_loss": round(locked_log_loss, 6),
            "baseline_log_loss": round(baseline_log_loss, 6),
            "market_baseline_brier": round(locked_market["market_brier"], 6),
            "market_baseline_log_loss": round(locked_market["market_log_loss"], 6),
            "market_baseline_coverage": round(locked_market["coverage"], 6),
            "candidate_brier_on_market_rows": round(
                locked_market["candidate_brier"], 6
            ),
            "candidate_log_loss_on_market_rows": round(
                locked_market["candidate_log_loss"], 6
            ),
        },
        "acceptance_checks": acceptance_checks,
        "notice": (
            "The historical audit must beat both 50% and the sportsbook closing no-vig baseline. A rejected artifact cannot create picks."
        ),
    }


def _market_pairs(
    db: sqlite3.Connection,
    *,
    game: sqlite3.Row,
    as_of: datetime,
    stale_after_minutes: int,
) -> dict[float, list[dict[str, Any]]]:
    oldest = as_of - timedelta(minutes=stale_after_minutes)
    rows = db.execute(
        """
        SELECT * FROM odds_snapshots
        WHERE game_id = ? AND market_key = 'spreads' AND period = 'full_game'
          AND market_status = 'open'
          AND source_updated_at <= ? AND collected_at <= ?
          AND source_updated_at >= ? AND collected_at >= ?
          AND (is_accessible = 1 OR is_consensus_eligible = 1)
        ORDER BY source_updated_at DESC, collected_at DESC, odds_snapshot_id DESC
        """,
        (
            game["game_id"],
            _iso(as_of),
            _iso(as_of),
            _iso(oldest),
            _iso(oldest),
        ),
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        key = (
            str(row["sportsbook_key"]),
            str(row["settlement_rule"]),
            str(row["source_updated_at"]),
            str(row["collected_at"]),
        )
        grouped.setdefault(key, []).append(row)
    by_line: dict[float, list[dict[str, Any]]] = {}
    seen_books: set[tuple[str, float]] = set()
    for key, quotes in grouped.items():
        home = next(
            (row for row in quotes if row["outcome_team_id"] == game["home_team_id"]),
            None,
        )
        away = next(
            (row for row in quotes if row["outcome_team_id"] == game["away_team_id"]),
            None,
        )
        if home is None or away is None:
            continue
        home_line = float(home["spread_points"])
        if abs(home_line + float(away["spread_points"])) > 1e-9:
            continue
        book_line = (str(home["sportsbook_key"]), home_line)
        if book_line in seen_books:
            continue
        seen_books.add(book_line)
        probabilities = remove_vig(
            (Decimal(str(home["decimal_odds"])), Decimal(str(away["decimal_odds"])))
        )
        by_line.setdefault(home_line, []).append(
            {
                "sportsbook": str(home["sportsbook_key"]),
                "home_no_vig": float(probabilities[0]),
                "updated_at": str(home["source_updated_at"]),
            }
        )
    return by_line


def _choose_line(by_line: dict[float, list[dict[str, Any]]]) -> tuple[float, list[dict[str, Any]]] | None:
    if not by_line:
        return None
    return max(
        by_line.items(),
        key=lambda item: (
            len(item[1]),
            max(value["updated_at"] for value in item[1]),
            -abs(item[0]),
        ),
    )


def build_current_model_runs(
    db: sqlite3.Connection,
    *,
    artifact: ModelArtifact,
    as_of: datetime,
    hours: int = DEFAULT_WINDOW_HOURS,
    game_id: str | None = None,
    stale_after_minutes: int = 60,
    persist: bool = True,
) -> dict[str, Any]:
    if artifact.status != "acceptable":
        return {
            "status": "blocked",
            "reason": "The market-benchmarked historical audit rejected the model artifact.",
            "predictions_stored": 0,
        }
    as_of = as_of.astimezone(timezone.utc)
    end = as_of + timedelta(hours=hours)
    parameters: list[Any] = [_iso(as_of), _iso(end)]
    filter_sql = ""
    if game_id:
        filter_sql = " AND g.game_id = ?"
        parameters.append(game_id)
    games = db.execute(
        f"""
        SELECT g.* FROM games g
        WHERE g.status = 'scheduled' AND g.kickoff_time >= ? AND g.kickoff_time <= ?
        {filter_sql}
        ORDER BY g.kickoff_time, g.game_id
        """,
        parameters,
    ).fetchall()
    _, ratings = _historical_examples(db)
    created: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    for game in games:
        selected = _choose_line(
            _market_pairs(
                db,
                game=game,
                as_of=as_of,
                stale_after_minutes=stale_after_minutes,
            )
        )
        if selected is None:
            passes.append(
                {"game_id": game["game_id"], "reason": "No fresh complete exact spread pair."}
            )
            continue
        home_spread, books = selected
        push_probability = artifact.push_rate(home_spread)
        if push_probability is None:
            passes.append(
                {
                    "game_id": game["game_id"],
                    "reason": "No responsible historical push estimate exists for this whole-number spread.",
                }
            )
            continue
        rating_difference = ratings.get(str(game["home_team_id"]), 1500.0) - ratings.get(
            str(game["away_team_id"]), 1500.0
        )
        conditional_home = artifact.probability(rating_difference, home_spread)
        home_probability = conditional_home * (1 - push_probability)
        away_probability = (1 - conditional_home) * (1 - push_probability)
        market_values = [item["home_no_vig"] for item in books]
        market_disagreement = (
            (max(market_values) - min(market_values)) / 2 if len(market_values) > 1 else 0.0
        )
        domain_guard = (
            PRESEASON_UNCERTAINTY_GUARD
            if game["season_type"] == "preseason"
            else REGULAR_UNCERTAINTY_FLOOR
        )
        uncertainty = min(
            0.25,
            max(artifact.validation_ece, artifact.locked_test_ece)
            + market_disagreement
            + domain_guard,
        )
        run_id = str(uuid.uuid4())
        data_version = hashlib.sha256(
            json.dumps(
                {
                    "history": artifact.data_hash,
                    "game_id": game["game_id"],
                    "line": home_spread,
                    "books": books,
                    "as_of": _iso(as_of),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if persist:
            with db:
                db.execute(
                    """
                    INSERT INTO model_runs (
                        model_run_id, game_id, model_version, feature_version,
                        data_version, rule_version, training_cutoff,
                        prediction_time, home_cover_probability,
                        away_cover_probability, push_probability,
                        uncertainty_margin, market_spread_points,
                        oos_decision_count, calibration_status, clv_status,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'acceptable', 'unknown', 'test_only', ?)
                    """,
                    (
                        run_id,
                        game["game_id"],
                        artifact.model_version,
                        artifact.feature_version,
                        data_version,
                        artifact.rule_version,
                        _iso(artifact.training_cutoff),
                        _iso(as_of),
                        home_probability,
                        away_probability,
                        push_probability,
                        uncertainty,
                        home_spread,
                        artifact.locked_test_count,
                        _iso(as_of),
                    ),
                )
                db.execute(
                    """
                    INSERT INTO audit_log (
                        entity_type, entity_id, action, reason, new_value_json,
                        responsible_actor, occurred_at
                    ) VALUES ('model_run', ?, 'created', ?, ?, 'phase4_model', ?)
                    """,
                    (
                        run_id,
                        "Interpretable Elo margin model passed the market-benchmarked historical audit and used only data available by prediction time.",
                        json.dumps(
                            {
                                "game_id": game["game_id"],
                                "market_spread_points": home_spread,
                                "rating_difference": rating_difference,
                                "home_cover_probability": home_probability,
                                "away_cover_probability": away_probability,
                                "push_probability": push_probability,
                                "uncertainty_margin": uncertainty,
                            },
                            sort_keys=True,
                        ),
                        _iso(as_of),
                    ),
                )
        created.append(
            {
                "game_id": game["game_id"],
                "matchup": f'{game["away_team_id"]} at {game["home_team_id"]}',
                "market_spread_points": home_spread,
                "home_cover_probability": round(home_probability, 6),
                "away_cover_probability": round(away_probability, 6),
                "push_probability": round(push_probability, 6),
                "uncertainty_margin": round(uncertainty, 6),
                "market_books": len(books),
                "model_run_id": run_id if persist else None,
                "status": "test_only",
            }
        )
    return {
        "status": "completed",
        "games_found": len(games),
        "predictions_stored": len(created) if persist else 0,
        "predictions_previewed": len(created),
        "predictions": created,
        "passes": passes,
        "notice": "No real-money action is taken. Preseason model runs remain test-only.",
    }


def run_phase4(
    db: sqlite3.Connection,
    *,
    as_of: datetime,
    hours: int = DEFAULT_WINDOW_HOURS,
    game_id: str | None = None,
    stale_after_minutes: int = 60,
    persist: bool = True,
) -> dict[str, Any]:
    artifact, training = train_first_model(
        db, as_of=as_of, persist=persist
    )
    if artifact is None or artifact.status != "acceptable":
        return {"training": training, "prediction": {"status": "not_run"}}
    prediction = build_current_model_runs(
        db,
        artifact=artifact,
        as_of=as_of,
        hours=hours,
        game_id=game_id,
        stale_after_minutes=stale_after_minutes,
        persist=persist,
    )
    return {"training": training, "prediction": prediction}
