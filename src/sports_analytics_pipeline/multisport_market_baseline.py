"""Market-implied engineering baseline for untrained multi-sport paths.

This module exists only to make an otherwise untrained sport exercise the complete
paper pipeline. It does not replace the approved sport-specific model, does not count
toward the 100-decision validation sample, and cannot authorize wagering.

The baseline uses the median no-vig probability from the current matching bookmaker
market. For spread sports it also locks the most widely quoted exact spread before
creating the prediction. Missing or inconsistent current markets fail closed.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from statistics import median
from uuid import uuid4

from .multisport_evaluation import MarketView, current_market_views
from .multisport_runtime import ensure_runtime_schema, record_feature
from .sports_registry import get_sport_market


UTC = timezone.utc
BASELINE_VERSION = "market-baseline-1.0"
BASELINE_FEATURE_VERSION = "market-baseline-features-1.0"
BASELINE_UNCERTAINTY = 0.02
SUPPORTED_SPORTS = frozenset({"nba", "wnba", "mma", "atp", "wta"})


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def baseline_model_version(sport: str) -> str:
    key = sport.casefold().strip()
    if key not in SUPPORTED_SPORTS:
        raise ValueError(f"Market baseline is not approved for {sport!r}")
    return f"{key}-{BASELINE_VERSION}"


def is_market_baseline_model(connection: sqlite3.Connection, model_version: str) -> bool:
    row = connection.execute(
        "SELECT feature_version, parameters_json FROM multisport_model_artifacts WHERE model_version=?",
        (model_version,),
    ).fetchone()
    if row is None or str(row["feature_version"]) != BASELINE_FEATURE_VERSION:
        return False
    try:
        parameters = json.loads(str(row["parameters_json"]))
    except Exception:
        return False
    return parameters.get("engineering_market_baseline") is True


def ensure_market_baseline_model(
    connection: sqlite3.Connection,
    *,
    sport: str,
    as_of: datetime,
) -> sqlite3.Row:
    """Persist a clearly labeled zero-training engineering model artifact."""
    ensure_runtime_schema(connection)
    key = sport.casefold().strip()
    if key not in SUPPORTED_SPORTS:
        raise ValueError(f"Market baseline is not approved for {sport!r}")
    spec = get_sport_market(key)
    version = baseline_model_version(key)
    feature_names = ["market_home_no_vig"]
    if spec.provider_market_key == "spreads":
        feature_names.insert(0, "market_spread_points")
    data_hash = sha256(f"{key}:{BASELINE_VERSION}".encode("utf-8")).hexdigest()
    now = _iso(as_of)
    connection.execute(
        """
        INSERT INTO multisport_model_artifacts (
            model_version, sport, market_key, period, feature_version, algorithm,
            feature_names_json, parameters_json, trained_at, training_cutoff,
            training_count, holdout_count, holdout_log_loss, holdout_brier,
            calibration_error, base_uncertainty, data_hash, status, notes_json
        ) VALUES (?, ?, ?, ?, ?, 'binary_logistic', ?, ?, ?, ?, 0, 0, 0, 0, 0, ?, ?, 'test_only', ?)
        ON CONFLICT(model_version) DO NOTHING
        """,
        (
            version,
            key,
            spec.internal_market_key,
            spec.period,
            BASELINE_FEATURE_VERSION,
            json.dumps(feature_names),
            json.dumps({
                "engineering_market_baseline": True,
                "method": "median_current_market_no_vig",
                "validation_countable": False,
                "feature_names": feature_names,
            }, sort_keys=True),
            now,
            now,
            BASELINE_UNCERTAINTY,
            data_hash,
            json.dumps([
                "Engineering fallback only when no trained sport model exists",
                "Uses current matching market consensus; no fabricated historical data",
                "Decisions from this model are not countable toward forward OOS validation",
                "Test-only; cannot authorize wagering",
            ]),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM multisport_model_artifacts WHERE model_version=?",
        (version,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Market baseline model artifact was not created")
    return row


def _spread_home_points(view: MarketView) -> float:
    row = next(row for row in view.rows if str(row["outcome_role"]) == "home")
    if row["points"] is None:
        raise ValueError("Spread baseline requires an exact home spread")
    return float(row["points"])


def _baseline_views(views: tuple[MarketView, ...], sport: str) -> tuple[MarketView, ...]:
    spec = get_sport_market(sport)
    if not views:
        raise ValueError("No fresh complete market is available for the engineering baseline")
    if spec.provider_market_key != "spreads":
        return views
    points = [_spread_home_points(view) for view in views]
    counts = Counter(points)
    max_count = max(counts.values())
    selected_points = min(
        (point for point, count in counts.items() if count == max_count),
        key=lambda point: (abs(point), point),
    )
    matched = tuple(view for view in views if abs(_spread_home_points(view) - selected_points) <= 1e-9)
    if not matched:
        raise ValueError("No matching spread views remain for the engineering baseline")
    return matched


def create_market_baseline_prediction(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    as_of: datetime,
    stale_after_minutes: int,
) -> str:
    """Create a timestamped market-implied prediction for engineering paper testing."""
    ensure_runtime_schema(connection)
    key = sport.casefold().strip()
    artifact = ensure_market_baseline_model(connection, sport=key, as_of=as_of)
    event = connection.execute(
        "SELECT * FROM multisport_events WHERE event_id=? AND sport=?",
        (event_id, key),
    ).fetchone()
    if event is None:
        raise ValueError("Unknown market-baseline event")
    commence = datetime.fromisoformat(str(event["commence_time"]).replace("Z", "+00:00"))
    if commence.tzinfo is None:
        commence = commence.replace(tzinfo=UTC)
    if as_of.astimezone(UTC) >= commence.astimezone(UTC):
        raise ValueError("Market baseline prediction must precede event start")

    views = current_market_views(
        connection,
        event_id=event_id,
        sport=key,
        as_of=as_of,
        stale_after_minutes=stale_after_minutes,
    )
    matched = _baseline_views(views, key)
    roles = ("home", "away")
    probabilities = {
        role: float(median(view.probabilities[role] for view in matched))
        for role in roles
    }
    total = sum(probabilities.values())
    if total <= 0:
        raise ValueError("Market baseline probability total is invalid")
    probabilities = {role: value / total for role, value in probabilities.items()}

    spec = get_sport_market(key)
    source_updated_at = max(view.collected_at for view in matched)
    if spec.provider_market_key == "spreads":
        record_feature(
            connection,
            event_id=event_id,
            sport=key,
            feature_name="market_spread_points",
            feature_value=_spread_home_points(matched[0]),
            cutoff_time=as_of,
            feature_version=BASELINE_FEATURE_VERSION,
            collected_at=as_of,
            source_key="the_odds_api_market_baseline",
            source_updated_at=source_updated_at,
        )
    record_feature(
        connection,
        event_id=event_id,
        sport=key,
        feature_name="market_home_no_vig",
        feature_value=probabilities["home"],
        cutoff_time=as_of,
        feature_version=BASELINE_FEATURE_VERSION,
        collected_at=as_of,
        source_key="the_odds_api_market_baseline",
        source_updated_at=source_updated_at,
    )

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
            prediction_id,
            event_id,
            str(artifact["model_version"]),
            key,
            spec.internal_market_key,
            spec.period,
            _iso(as_of),
            _iso(as_of),
            json.dumps(probabilities, sort_keys=True),
            BASELINE_UNCERTAINTY,
            _iso(as_of),
        ),
    )
    connection.commit()
    return prediction_id
