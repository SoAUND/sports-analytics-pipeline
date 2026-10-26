"""Our MLB Phase 4 first interpretable moneyline model.

This model is deliberately small and auditable. It uses only pregame team Elo
ratings derived from earlier final MLB games plus a learned home-field term.
Development is 2020-2023, calibration is 2024, the locked historical audit is
2025, and 2026 is reserved for forward paper decisions/rating updates.

Historical prediction-time sportsbook prices are not available in the free
workflow, so this phase does not claim to beat a historical market baseline.
Every generated model run remains test_only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import statistics
import uuid
from typing import Iterable

MODEL_VERSION = "our-mlb-elo-moneyline-v1.0"
FEATURE_VERSION = "mlb-pregame-team-elo-v1.0"
RULE_VERSION = "our-mlb-first-model-v1.0"
DEVELOPMENT_SEASONS = (2020, 2021, 2022, 2023)
VALIDATION_SEASON = 2024
LOCKED_TEST_SEASON = 2025
FORWARD_SEASON = 2026
DEFAULT_ELO = 1500.0
ELO_K = 20.0
MINIMUM_DEVELOPMENT_GAMES = 1000
MINIMUM_VALIDATION_GAMES = 300
MINIMUM_LOCKED_TEST_GAMES = 300
UNCERTAINTY_FLOOR = 0.02


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
        return 1.0 / (1.0 + factor)
    factor = math.exp(value)
    return factor / (1.0 + factor)


def _logit(probability: float) -> float:
    probability = _clamp(probability)
    return math.log(probability / (1.0 - probability))


@dataclass(frozen=True)
class MlbHistoricalExample:
    source_game_id: str
    season: int
    kickoff_time: datetime
    home_team_id: str
    away_team_id: str
    elo_difference: float
    home_win: int


@dataclass(frozen=True)
class MlbModelArtifact:
    model_version: str
    feature_version: str
    rule_version: str
    trained_at: datetime
    training_cutoff: datetime
    intercept: float
    elo_coefficient: float
    calibration_intercept: float
    calibration_slope: float
    validation_brier: float
    validation_log_loss: float
    validation_ece: float
    locked_test_brier: float
    locked_test_log_loss: float
    locked_test_ece: float
    baseline_brier: float
    baseline_log_loss: float
    development_count: int
    validation_count: int
    locked_test_count: int
    data_hash: str
    calibration_status: str
    market_audit_status: str
    status: str

    def probability(self, elo_difference: float) -> float:
        raw = _sigmoid(self.intercept + self.elo_coefficient * (elo_difference / 400.0))
        return _sigmoid(
            self.calibration_intercept + self.calibration_slope * _logit(raw)
        )

    @property
    def uncertainty_margin(self) -> float:
        return max(UNCERTAINTY_FLOOR, self.validation_ece, self.locked_test_ece)


def _elo_examples(
    rows: Iterable[sqlite3.Row],
    *,
    cutoff: datetime | None = None,
) -> tuple[list[MlbHistoricalExample], dict[str, float]]:
    ratings: dict[str, float] = {}
    examples: list[MlbHistoricalExample] = []
    for row in rows:
        kickoff = _parse(str(row["kickoff_time"]))
        if cutoff is not None and kickoff >= cutoff:
            break
        home = str(row["home_team_id"])
        away = str(row["away_team_id"])
        home_rating = ratings.get(home, DEFAULT_ELO)
        away_rating = ratings.get(away, DEFAULT_ELO)
        difference = home_rating - away_rating
        home_win = 1 if int(row["home_score"]) > int(row["away_score"]) else 0
        examples.append(
            MlbHistoricalExample(
                source_game_id=str(row["source_game_id"]),
                season=int(row["season"]),
                kickoff_time=kickoff,
                home_team_id=home,
                away_team_id=away,
                elo_difference=difference,
                home_win=home_win,
            )
        )
        expected = _sigmoid(math.log(10) * difference / 400.0)
        change = ELO_K * (home_win - expected)
        ratings[home] = home_rating + change
        ratings[away] = away_rating - change
    return examples, ratings


def historical_examples(
    db: sqlite3.Connection, *, cutoff: datetime | None = None
) -> tuple[list[MlbHistoricalExample], dict[str, float]]:
    rows = db.execute(
        """
        SELECT source_game_id, season, kickoff_time, home_team_id, away_team_id,
               home_score, away_score
        FROM historical_mlb_games
        ORDER BY kickoff_time, source_game_id
        """
    ).fetchall()
    return _elo_examples(rows, cutoff=cutoff)


def _fit_logistic(features: list[float], outcomes: list[int]) -> tuple[float, float]:
    if len(features) != len(outcomes) or len(features) < 3:
        raise ValueError("At least three aligned games are required")
    intercept = 0.0
    slope = 1.0
    regularization = 1e-4
    for _ in range(80):
        g0 = 0.0
        g1 = -regularization * slope
        h00 = 0.0
        h01 = 0.0
        h11 = regularization
        for feature, outcome in zip(features, outcomes):
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
        d0 = (g0 * h11 - g1 * h01) / determinant
        d1 = (g1 * h00 - g0 * h01) / determinant
        intercept += d0
        slope += d1
        if max(abs(d0), abs(d1)) < 1e-8:
            break
    return intercept, slope


def _brier(probabilities: list[float], outcomes: list[int]) -> float:
    return statistics.fmean((p - y) ** 2 for p, y in zip(probabilities, outcomes))


def _log_loss(probabilities: list[float], outcomes: list[int]) -> float:
    return statistics.fmean(
        -(y * math.log(_clamp(p)) + (1 - y) * math.log(_clamp(1 - p)))
        for p, y in zip(probabilities, outcomes)
    )


def _ece(probabilities: list[float], outcomes: list[int], bins: int = 10) -> float:
    groups: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for probability, outcome in zip(probabilities, outcomes):
        groups[min(bins - 1, int(probability * bins))].append((probability, outcome))
    total = len(probabilities)
    return sum(
        (len(group) / total)
        * abs(statistics.fmean(p for p, _ in group) - statistics.fmean(y for _, y in group))
        for group in groups
        if group
    )


def _hash_examples(examples: list[MlbHistoricalExample]) -> str:
    payload = [
        {
            **asdict(item),
            "kickoff_time": _iso(item.kickoff_time),
        }
        for item in examples
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def train_mlb_model(
    db: sqlite3.Connection,
    *,
    trained_at: datetime,
    minimum_development_games: int = MINIMUM_DEVELOPMENT_GAMES,
    minimum_validation_games: int = MINIMUM_VALIDATION_GAMES,
    minimum_locked_test_games: int = MINIMUM_LOCKED_TEST_GAMES,
) -> MlbModelArtifact:
    examples, _ratings = historical_examples(db)
    development = [item for item in examples if item.season in DEVELOPMENT_SEASONS]
    validation = [item for item in examples if item.season == VALIDATION_SEASON]
    locked = [item for item in examples if item.season == LOCKED_TEST_SEASON]
    if len(development) < minimum_development_games:
        raise ValueError("Insufficient MLB development history")
    if len(validation) < minimum_validation_games:
        raise ValueError("Insufficient MLB validation history")
    if len(locked) < minimum_locked_test_games:
        raise ValueError("Insufficient MLB locked-test history")

    x_dev = [item.elo_difference / 400.0 for item in development]
    y_dev = [item.home_win for item in development]
    intercept, slope = _fit_logistic(x_dev, y_dev)

    raw_validation = [
        _sigmoid(intercept + slope * (item.elo_difference / 400.0))
        for item in validation
    ]
    y_validation = [item.home_win for item in validation]
    calibration_intercept, calibration_slope = _fit_logistic(
        [_logit(p) for p in raw_validation], y_validation
    )

    def calibrated(item: MlbHistoricalExample) -> float:
        raw = _sigmoid(intercept + slope * (item.elo_difference / 400.0))
        return _sigmoid(calibration_intercept + calibration_slope * _logit(raw))

    validation_probs = [calibrated(item) for item in validation]
    locked_probs = [calibrated(item) for item in locked]
    locked_outcomes = [item.home_win for item in locked]
    baseline_probs = [0.5] * len(locked)

    validation_brier = _brier(validation_probs, y_validation)
    validation_log_loss = _log_loss(validation_probs, y_validation)
    validation_ece = _ece(validation_probs, y_validation)
    locked_brier = _brier(locked_probs, locked_outcomes)
    locked_log_loss = _log_loss(locked_probs, locked_outcomes)
    locked_ece = _ece(locked_probs, locked_outcomes)
    baseline_brier = _brier(baseline_probs, locked_outcomes)
    baseline_log_loss = _log_loss(baseline_probs, locked_outcomes)

    calibration_status = (
        "acceptable"
        if validation_ece <= 0.08 and locked_brier <= baseline_brier
        else "unacceptable"
    )
    training_cutoff = max(item.kickoff_time for item in locked)
    artifact = MlbModelArtifact(
        model_version=MODEL_VERSION,
        feature_version=FEATURE_VERSION,
        rule_version=RULE_VERSION,
        trained_at=trained_at.astimezone(timezone.utc),
        training_cutoff=training_cutoff,
        intercept=intercept,
        elo_coefficient=slope,
        calibration_intercept=calibration_intercept,
        calibration_slope=calibration_slope,
        validation_brier=validation_brier,
        validation_log_loss=validation_log_loss,
        validation_ece=validation_ece,
        locked_test_brier=locked_brier,
        locked_test_log_loss=locked_log_loss,
        locked_test_ece=locked_ece,
        baseline_brier=baseline_brier,
        baseline_log_loss=baseline_log_loss,
        development_count=len(development),
        validation_count=len(validation),
        locked_test_count=len(locked),
        data_hash=_hash_examples(examples),
        calibration_status=calibration_status,
        market_audit_status="missing_historical_prediction_time_prices",
        status="test_only",
    )
    with db:
        db.execute(
            """
            INSERT INTO mlb_model_artifacts (
                model_version, feature_version, rule_version, trained_at, training_cutoff,
                development_count, validation_count, locked_test_count, coefficients_json,
                calibration_json, metrics_json, data_hash, calibration_status,
                market_audit_status, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_version) DO UPDATE SET
                feature_version=excluded.feature_version,
                rule_version=excluded.rule_version,
                trained_at=excluded.trained_at,
                training_cutoff=excluded.training_cutoff,
                development_count=excluded.development_count,
                validation_count=excluded.validation_count,
                locked_test_count=excluded.locked_test_count,
                coefficients_json=excluded.coefficients_json,
                calibration_json=excluded.calibration_json,
                metrics_json=excluded.metrics_json,
                data_hash=excluded.data_hash,
                calibration_status=excluded.calibration_status,
                market_audit_status=excluded.market_audit_status,
                status=excluded.status
            """,
            (
                artifact.model_version,
                artifact.feature_version,
                artifact.rule_version,
                _iso(artifact.trained_at),
                _iso(artifact.training_cutoff),
                artifact.development_count,
                artifact.validation_count,
                artifact.locked_test_count,
                json.dumps({"intercept": artifact.intercept, "elo_coefficient": artifact.elo_coefficient}),
                json.dumps({"intercept": artifact.calibration_intercept, "slope": artifact.calibration_slope}),
                json.dumps({
                    "validation_brier": artifact.validation_brier,
                    "validation_log_loss": artifact.validation_log_loss,
                    "validation_ece": artifact.validation_ece,
                    "locked_test_brier": artifact.locked_test_brier,
                    "locked_test_log_loss": artifact.locked_test_log_loss,
                    "locked_test_ece": artifact.locked_test_ece,
                    "baseline_brier": artifact.baseline_brier,
                    "baseline_log_loss": artifact.baseline_log_loss,
                }),
                artifact.data_hash,
                artifact.calibration_status,
                artifact.market_audit_status,
                artifact.status,
            ),
        )
    return artifact


def load_mlb_artifact(db: sqlite3.Connection, model_version: str = MODEL_VERSION) -> MlbModelArtifact:
    row = db.execute(
        "SELECT * FROM mlb_model_artifacts WHERE model_version=?", (model_version,)
    ).fetchone()
    if row is None:
        raise ValueError("No stored MLB model artifact exists")
    coefficients = json.loads(row["coefficients_json"])
    calibration = json.loads(row["calibration_json"])
    metrics = json.loads(row["metrics_json"])
    return MlbModelArtifact(
        model_version=row["model_version"],
        feature_version=row["feature_version"],
        rule_version=row["rule_version"],
        trained_at=_parse(row["trained_at"]),
        training_cutoff=_parse(row["training_cutoff"]),
        intercept=float(coefficients["intercept"]),
        elo_coefficient=float(coefficients["elo_coefficient"]),
        calibration_intercept=float(calibration["intercept"]),
        calibration_slope=float(calibration["slope"]),
        validation_brier=float(metrics["validation_brier"]),
        validation_log_loss=float(metrics["validation_log_loss"]),
        validation_ece=float(metrics["validation_ece"]),
        locked_test_brier=float(metrics["locked_test_brier"]),
        locked_test_log_loss=float(metrics["locked_test_log_loss"]),
        locked_test_ece=float(metrics["locked_test_ece"]),
        baseline_brier=float(metrics["baseline_brier"]),
        baseline_log_loss=float(metrics["baseline_log_loss"]),
        development_count=int(row["development_count"]),
        validation_count=int(row["validation_count"]),
        locked_test_count=int(row["locked_test_count"]),
        data_hash=row["data_hash"],
        calibration_status=row["calibration_status"],
        market_audit_status=row["market_audit_status"],
        status=row["status"],
    )


def create_mlb_model_run(
    db: sqlite3.Connection,
    *,
    game_id: str,
    prediction_time: datetime,
    artifact: MlbModelArtifact | None = None,
) -> str:
    """Create one immutable test-only MLB moneyline model run for an upcoming game."""

    artifact = artifact or load_mlb_artifact(db)
    prediction_time = prediction_time.astimezone(timezone.utc)
    game = db.execute(
        "SELECT * FROM games WHERE game_id=? AND sport='mlb'", (game_id,)
    ).fetchone()
    if game is None:
        raise ValueError("MLB game not found")
    kickoff = _parse(str(game["kickoff_time"]))
    if prediction_time >= kickoff:
        raise ValueError("MLB model run must be created before game start")
    if artifact.training_cutoff > prediction_time:
        raise ValueError("Model artifact training cutoff cannot follow prediction time")
    _examples, ratings = historical_examples(db, cutoff=prediction_time)
    home_rating = ratings.get(str(game["home_team_id"]), DEFAULT_ELO)
    away_rating = ratings.get(str(game["away_team_id"]), DEFAULT_ELO)
    home_probability = artifact.probability(home_rating - away_rating)
    away_probability = 1.0 - home_probability
    run_id = f"mlb_model_{uuid.uuid4().hex}"
    with db:
        db.execute(
            """
            INSERT INTO model_runs (
                model_run_id, game_id, sport, market_key, model_version, feature_version,
                data_version, rule_version, training_cutoff, prediction_time,
                home_cover_probability, away_cover_probability, push_probability,
                uncertainty_margin, market_spread_points, oos_decision_count,
                calibration_status, clv_status, status, created_at
            ) VALUES (?, ?, 'mlb', 'moneylines', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 0, ?, 'unknown', 'test_only', ?)
            """,
            (
                run_id,
                game_id,
                artifact.model_version,
                artifact.feature_version,
                artifact.data_hash,
                artifact.rule_version,
                _iso(artifact.training_cutoff),
                _iso(prediction_time),
                home_probability,
                away_probability,
                artifact.uncertainty_margin,
                artifact.calibration_status,
                _iso(prediction_time),
            ),
        )
    return run_id


def create_upcoming_mlb_model_runs(
    db: sqlite3.Connection,
    *,
    prediction_time: datetime,
    hours: int = 24,
    game_id: str | None = None,
    eligible_game_ids: Iterable[str] | None = None,
) -> dict[str, object]:
    artifact = load_mlb_artifact(db)
    start = prediction_time.astimezone(timezone.utc)
    from datetime import timedelta
    end = start + timedelta(hours=hours)
    filters = [
        "sport='mlb'",
        "season_type='regular'",
        "kickoff_time > ?",
        "kickoff_time <= ?",
    ]
    params: list[object] = [_iso(start), _iso(end)]
    if game_id is not None:
        filters.append("game_id = ?")
        params.append(game_id)
    allowed = tuple(dict.fromkeys(str(value) for value in (eligible_game_ids or ())))
    if eligible_game_ids is not None:
        if not allowed:
            games = []
        else:
            placeholders = ",".join("?" for _ in allowed)
            filters.append(f"game_id IN ({placeholders})")
            params.extend(allowed)
            games = db.execute(
                f"""
                SELECT game_id FROM games
                WHERE {' AND '.join(filters)}
                ORDER BY kickoff_time, game_id
                """,
                params,
            ).fetchall()
    else:
        games = db.execute(
            f"""
            SELECT game_id FROM games
            WHERE {' AND '.join(filters)}
            ORDER BY kickoff_time, game_id
            """,
            params,
        ).fetchall()
    created: list[str] = []
    for row in games:
        existing = db.execute(
            """
            SELECT model_run_id FROM model_runs
            WHERE game_id=? AND sport='mlb' AND market_key='moneylines' AND prediction_time=?
            """,
            (row["game_id"], _iso(start)),
        ).fetchone()
        if existing is not None:
            created.append(str(existing["model_run_id"]))
            continue
        created.append(
            create_mlb_model_run(
                db, game_id=str(row["game_id"]), prediction_time=start, artifact=artifact
            )
        )
    return {
        "mode": "test_only",
        "model_version": artifact.model_version,
        "market_audit_status": artifact.market_audit_status,
        "games_found": len(games),
        "model_runs": created,
        "notice": (
            "MLB model runs are test-only. Historical prediction-time moneyline prices are still "
            "required before the model can be benchmarked against the market."
        ),
    }
