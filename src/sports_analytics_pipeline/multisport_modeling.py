"""Transparent probability models for our controlled multi-sport markets.

NBA/WNBA spreads use binary logistic cover models with a separately measured push
rate. Premier League uses a three-class softmax model. UFC/MMA, ATP, and WTA use
binary logistic winner models. Every model is chronological, auditable, and test-only
until its own forward validation bucket passes manual review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import sqlite3
from uuid import uuid4

from .multisport_features import (
    EPL_BASELINE_FEATURE_VERSION,
    FEATURE_VERSION,
    required_feature_names,
)
from .multisport_runtime import ensure_runtime_schema, feature_vector
from .sports_registry import get_sport_market


UTC = timezone.utc
EPS = 1e-12


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _softmax(values: list[float]) -> list[float]:
    maximum = max(values)
    exps = [math.exp(value - maximum) for value in values]
    total = sum(exps)
    return [value / total for value in exps]


def _standardizer(rows: list[dict[str, float]], names: list[str]) -> tuple[list[float], list[float]]:
    means: list[float] = []
    scales: list[float] = []
    for name in names:
        values = [float(row[name]) for row in rows]
        mean_value = sum(values) / len(values)
        variance = sum((value - mean_value) ** 2 for value in values) / len(values)
        scale = math.sqrt(variance)
        means.append(mean_value)
        scales.append(scale if scale > 1e-9 else 1.0)
    return means, scales


def _vectorize(
    features: dict[str, float],
    names: list[str],
    means: list[float],
    scales: list[float],
) -> list[float]:
    missing = [name for name in names if name not in features]
    extra = [name for name in features if name not in names]
    if missing:
        raise ValueError(f"Missing required model features: {', '.join(missing)}")
    if extra:
        raise ValueError(f"Unexpected model features: {', '.join(sorted(extra))}")
    return [1.0] + [
        (float(features[name]) - mean_value) / scale
        for name, mean_value, scale in zip(names, means, scales)
    ]


def _fit_binary(
    rows: list[tuple[list[float], int]],
    *,
    iterations: int = 1500,
    learning_rate: float = 0.05,
    l2: float = 0.001,
) -> list[float]:
    weights = [0.0] * len(rows[0][0])
    for _ in range(iterations):
        gradient = [0.0] * len(weights)
        for vector, label in rows:
            p = _sigmoid(sum(a * b for a, b in zip(weights, vector)))
            error = p - label
            for index, value in enumerate(vector):
                gradient[index] += error * value
        for index in range(len(weights)):
            penalty = 0.0 if index == 0 else l2 * weights[index]
            weights[index] -= learning_rate * ((gradient[index] / len(rows)) + penalty)
    return weights


def _fit_softmax(
    rows: list[tuple[list[float], int]],
    class_count: int,
    *,
    iterations: int = 2000,
    learning_rate: float = 0.03,
    l2: float = 0.001,
) -> list[list[float]]:
    weights = [[0.0] * len(rows[0][0]) for _ in range(class_count)]
    for _ in range(iterations):
        gradient = [[0.0] * len(rows[0][0]) for _ in range(class_count)]
        for vector, label in rows:
            probs = _softmax([sum(a * b for a, b in zip(row, vector)) for row in weights])
            for class_index in range(class_count):
                error = probs[class_index] - (1.0 if label == class_index else 0.0)
                for index, value in enumerate(vector):
                    gradient[class_index][index] += error * value
        for class_index in range(class_count):
            for index in range(len(weights[class_index])):
                penalty = 0.0 if index == 0 else l2 * weights[class_index][index]
                weights[class_index][index] -= learning_rate * (
                    gradient[class_index][index] / len(rows) + penalty
                )
    return weights


def _binary_metrics(rows: list[tuple[list[float], int]], weights: list[float]) -> tuple[float, float, float]:
    if not rows:
        raise ValueError("Metrics require holdout rows")
    log_loss = 0.0
    brier = 0.0
    bins: list[list[tuple[float, int]]] = [[] for _ in range(10)]
    for vector, label in rows:
        p = min(1.0 - EPS, max(EPS, _sigmoid(sum(a * b for a, b in zip(weights, vector)))))
        log_loss += -(label * math.log(p) + (1 - label) * math.log(1 - p))
        brier += (p - label) ** 2
        bins[min(9, int(p * 10))].append((p, label))
    calibration = 0.0
    for bucket in bins:
        if bucket:
            calibration += len(bucket) / len(rows) * abs(
                sum(item[0] for item in bucket) / len(bucket)
                - sum(item[1] for item in bucket) / len(bucket)
            )
    return log_loss / len(rows), brier / len(rows), calibration


def _softmax_metrics(
    rows: list[tuple[list[float], int]],
    weights: list[list[float]],
) -> tuple[float, float, float]:
    if not rows:
        raise ValueError("Metrics require holdout rows")
    log_loss = 0.0
    brier = 0.0
    class_bins: list[list[list[tuple[float, int]]]] = [
        [[] for _ in range(10)] for _ in range(len(weights))
    ]
    for vector, label in rows:
        probs = _softmax([sum(a * b for a, b in zip(row, vector)) for row in weights])
        log_loss += -math.log(max(EPS, probs[label]))
        for class_index, p in enumerate(probs):
            target = 1.0 if class_index == label else 0.0
            brier += (p - target) ** 2 / len(weights)
            class_bins[class_index][min(9, int(p * 10))].append((p, int(target)))
    class_errors: list[float] = []
    for bins in class_bins:
        error = 0.0
        for bucket in bins:
            if bucket:
                error += len(bucket) / len(rows) * abs(
                    sum(item[0] for item in bucket) / len(bucket)
                    - sum(item[1] for item in bucket) / len(bucket)
                )
        class_errors.append(error)
    return log_loss / len(rows), brier / len(rows), sum(class_errors) / len(class_errors)


@dataclass(frozen=True)
class TrainedModel:
    model_version: str
    sport: str
    market_key: str
    period: str
    feature_version: str
    algorithm: str
    training_count: int
    holdout_count: int
    holdout_log_loss: float
    holdout_brier: float
    calibration_error: float
    base_uncertainty: float


def _load_examples(
    connection: sqlite3.Connection,
    *,
    sport: str,
    feature_version: str,
    training_cutoff: datetime,
) -> list[sqlite3.Row]:
    spec = get_sport_market(sport)
    return connection.execute(
        """
        SELECT * FROM multisport_training_examples
        WHERE sport=? AND market_key=? AND period=? AND feature_version=?
          AND event_time<=? AND outcome_role<>'void'
        ORDER BY event_time, training_example_id
        """,
        (sport, spec.internal_market_key, spec.period, feature_version, _iso(training_cutoff)),
    ).fetchall()


def train_model(
    connection: sqlite3.Connection,
    *,
    sport: str,
    feature_version: str,
    training_cutoff: datetime,
    min_examples: int = 50,
    holdout_fraction: float = 0.20,
    trained_at: datetime | None = None,
) -> TrainedModel:
    ensure_runtime_schema(connection)
    if not 0.10 <= holdout_fraction <= 0.40:
        raise ValueError("holdout_fraction must be between 0.10 and 0.40")
    spec = get_sport_market(sport)
    examples = _load_examples(
        connection,
        sport=sport,
        feature_version=feature_version,
        training_cutoff=training_cutoff,
    )
    contract = set(required_feature_names(sport, feature_version=feature_version))
    usable: list[tuple[sqlite3.Row, dict[str, float]]] = []
    push_count = 0
    for row in examples:
        outcome = str(row["outcome_role"])
        allowed = {"home", "away", "draw"} if sport == "epl" else {"home", "away", "push"}
        if outcome not in allowed:
            raise ValueError(f"Unexpected {sport} training outcome: {outcome}")
        if outcome == "draw" and sport != "epl":
            raise ValueError("A binary model cannot silently treat a draw as an away win")
        if outcome == "push":
            if spec.provider_market_key != "spreads" and sport != "mma":
                raise ValueError("Push outcome is not approved for this training market")
            push_count += 1
            continue
        features = {k: float(v) for k, v in json.loads(row["features_json"]).items()}
        if set(features) != contract:
            missing = sorted(contract - set(features))
            extra = sorted(set(features) - contract)
            raise ValueError(f"Training feature contract mismatch; missing={missing}, extra={extra}")
        usable.append((row, features))
    if len(usable) < min_examples:
        raise ValueError(f"At least {min_examples} usable training examples are required; found {len(usable)}")

    feature_names = sorted(contract)
    split = max(1, min(len(usable) - 1, int(len(usable) * (1.0 - holdout_fraction))))
    train_items = usable[:split]
    holdout_items = usable[split:]
    if len(holdout_items) < 10:
        raise ValueError("Chronological holdout requires at least 10 examples")
    means, scales = _standardizer([features for _, features in train_items], feature_names)

    if sport == "epl":
        algorithm = "softmax"
        labels = ["home", "draw", "away"]
        label_index = {label: index for index, label in enumerate(labels)}
        train_rows = [
            (_vectorize(features, feature_names, means, scales), label_index[str(row["outcome_role"])])
            for row, features in train_items
        ]
        holdout_rows = [
            (_vectorize(features, feature_names, means, scales), label_index[str(row["outcome_role"])])
            for row, features in holdout_items
        ]
        if {label for _, label in train_rows} != {0, 1, 2}:
            raise ValueError("Premier League training data must contain home, draw, and away outcomes")
        weights = _fit_softmax(train_rows, 3)
        log_loss, brier, calibration = _softmax_metrics(holdout_rows, weights)
        parameters: dict[str, object] = {
            "labels": labels,
            "weights": weights,
            "means": means,
            "scales": scales,
            "push_rate": 0.0,
            "feature_names": feature_names,
        }
    else:
        algorithm = "binary_logistic"
        labels = ["home", "away"]
        train_rows = [
            (_vectorize(features, feature_names, means, scales), 1 if str(row["outcome_role"]) == "home" else 0)
            for row, features in train_items
        ]
        holdout_rows = [
            (_vectorize(features, feature_names, means, scales), 1 if str(row["outcome_role"]) == "home" else 0)
            for row, features in holdout_items
        ]
        if {label for _, label in train_rows} != {0, 1}:
            raise ValueError("Binary training data must contain both outcome sides")
        weights = _fit_binary(train_rows)
        log_loss, brier, calibration = _binary_metrics(holdout_rows, weights)
        push_rate = push_count / max(1, len(examples)) if spec.provider_market_key == "spreads" else 0.0
        parameters = {
            "labels": labels,
            "weights": weights,
            "means": means,
            "scales": scales,
            "push_rate": push_rate,
            "feature_names": feature_names,
        }

    base_uncertainty = min(0.15, max(0.02, calibration + 0.01))
    hash_payload = [
        {
            "id": int(row["training_example_id"]),
            "event_time": row["event_time"],
            "features": features,
            "outcome": row["outcome_role"],
        }
        for row, features in usable
    ]
    data_hash = sha256(json.dumps(hash_payload, sort_keys=True).encode("utf-8")).hexdigest()
    model_version = f"{sport}-{feature_version}-{training_cutoff.date().isoformat()}-{data_hash[:10]}"
    connection.execute(
        """
        INSERT INTO multisport_model_artifacts (
            model_version, sport, market_key, period, feature_version, algorithm,
            feature_names_json, parameters_json, trained_at, training_cutoff,
            training_count, holdout_count, holdout_log_loss, holdout_brier,
            calibration_error, base_uncertainty, data_hash, status, notes_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test_only', ?)
        ON CONFLICT(model_version) DO NOTHING
        """,
        (
            model_version, sport, spec.internal_market_key, spec.period, feature_version,
            algorithm, json.dumps(feature_names), json.dumps(parameters, sort_keys=True),
            _iso(trained_at), _iso(training_cutoff), len(train_items), len(holdout_items),
            log_loss, brier, calibration, base_uncertainty, data_hash,
            json.dumps([
                "Chronological holdout",
                "Exact sport feature contract",
                "No future information permitted",
                "Test-only until forward validation and manual review",
            ]),
        ),
    )
    connection.commit()
    return TrainedModel(
        model_version=model_version,
        sport=sport,
        market_key=spec.internal_market_key,
        period=spec.period,
        feature_version=feature_version,
        algorithm=algorithm,
        training_count=len(train_items),
        holdout_count=len(holdout_items),
        holdout_log_loss=log_loss,
        holdout_brier=brier,
        calibration_error=calibration,
        base_uncertainty=base_uncertainty,
    )


def model_probabilities(artifact: sqlite3.Row, features: dict[str, float]) -> dict[str, float]:
    parameters = json.loads(artifact["parameters_json"])
    names = [str(name) for name in parameters["feature_names"]]
    means = [float(value) for value in parameters["means"]]
    scales = [float(value) for value in parameters["scales"]]
    vector = _vectorize(features, names, means, scales)
    if artifact["algorithm"] == "softmax":
        weights = [[float(value) for value in row] for row in parameters["weights"]]
        probs = _softmax([sum(a * b for a, b in zip(row, vector)) for row in weights])
        result = {str(label): float(prob) for label, prob in zip(parameters["labels"], probs)}
    else:
        weights = [float(value) for value in parameters["weights"]]
        home_conditional = _sigmoid(sum(a * b for a, b in zip(weights, vector)))
        push_rate = float(parameters.get("push_rate", 0.0))
        non_push = 1.0 - push_rate
        result = {
            "home": home_conditional * non_push,
            "away": (1.0 - home_conditional) * non_push,
        }
        if push_rate > 0:
            result["push"] = push_rate
    if abs(sum(result.values()) - 1.0) > 1e-8:
        raise ValueError("Model probabilities do not sum to one")
    return result


def predict_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    model_version: str,
    prediction_time: datetime,
    feature_cutoff: datetime | None = None,
) -> str:
    ensure_runtime_schema(connection)
    artifact = connection.execute(
        "SELECT * FROM multisport_model_artifacts WHERE model_version=?",
        (model_version,),
    ).fetchone()
    if artifact is None:
        raise ValueError("Unknown model version")
    event = connection.execute("SELECT * FROM multisport_events WHERE event_id=?", (event_id,)).fetchone()
    if event is None:
        raise ValueError("Unknown event")
    if event["sport"] != artifact["sport"]:
        raise ValueError("Model sport does not match event")
    event_time = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=UTC)
    if prediction_time.astimezone(UTC) >= event_time.astimezone(UTC):
        raise ValueError("Prediction must be created before the event starts")
    cutoff = feature_cutoff or prediction_time
    if cutoff > prediction_time:
        raise ValueError("Feature cutoff cannot follow prediction time")
    model_feature_version = str(artifact["feature_version"])
    source_feature_version = (
        FEATURE_VERSION
        if model_feature_version == EPL_BASELINE_FEATURE_VERSION
        else model_feature_version
    )
    available_features = feature_vector(
        connection,
        event_id=event_id,
        cutoff_time=cutoff,
        feature_version=source_feature_version,
    )
    required = set(
        required_feature_names(
            str(event["sport"]),
            feature_version=model_feature_version,
        )
    )
    if not required <= set(available_features):
        raise ValueError("A complete approved feature vector is required before prediction")
    features = {name: available_features[name] for name in required}
    probabilities = model_probabilities(artifact, features)
    prediction_id = f"pred-{uuid4().hex}"
    connection.execute(
        """
        INSERT INTO multisport_predictions (
            prediction_id, event_id, model_version, sport, market_key, period,
            prediction_time, feature_cutoff, probabilities_json, uncertainty_margin,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test_only', ?)
        """,
        (
            prediction_id, event_id, model_version, artifact["sport"], artifact["market_key"],
            artifact["period"], _iso(prediction_time), _iso(cutoff),
            json.dumps(probabilities, sort_keys=True), float(artifact["base_uncertainty"]),
            _iso(prediction_time),
        ),
    )
    connection.commit()
    return prediction_id


def latest_model(connection: sqlite3.Connection, sport: str) -> sqlite3.Row | None:
    ensure_runtime_schema(connection)
    spec = get_sport_market(sport)
    return connection.execute(
        """
        SELECT * FROM multisport_model_artifacts
        WHERE sport=? AND market_key=? AND period=? AND status='test_only'
        ORDER BY trained_at DESC LIMIT 1
        """,
        (sport, spec.internal_market_key, spec.period),
    ).fetchone()
