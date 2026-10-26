"""Audited historical training import for our multi-sport models.

Historical data is never accepted merely because a result is present.  Every row must
include a pre-event source cutoff, event time, approved feature version, exact controlled
market, source name, and a complete approved feature payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
import csv
import io
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .multisport_features import (
    FEATURE_VERSION,
    training_feature_version,
    validate_feature_payload,
)
from .multisport_runtime import ensure_runtime_schema, record_training_example
from .sports_registry import get_sport_market


UTC = timezone.utc


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Historical timestamps must include a time zone")
    return parsed.astimezone(UTC)


def validate_history_row(row: dict[str, Any]) -> dict[str, Any]:
    required = {
        "sport", "provider_sport_key", "source_event_id", "event_time",
        "source_cutoff", "result_source_key", "outcome_role", "features",
    }
    missing = sorted(required - set(row))
    if missing:
        raise ValueError(f"Historical row is missing: {', '.join(missing)}")
    sport = str(row["sport"]).casefold().strip()
    spec = get_sport_market(sport)
    provider_key = str(row["provider_sport_key"])
    if not spec.matches_provider_sport_key(provider_key):
        raise ValueError("Historical provider key does not match the controlled sport")
    event_time = _dt(str(row["event_time"]))
    source_cutoff = _dt(str(row["source_cutoff"]))
    if source_cutoff >= event_time:
        raise ValueError("Historical feature cutoff must be strictly before event time")
    feature_version = str(row.get("feature_version") or FEATURE_VERSION)
    if feature_version != FEATURE_VERSION:
        raise ValueError("Historical row uses an unapproved feature version")
    features = validate_feature_payload(sport, dict(row["features"]))
    outcome = str(row["outcome_role"]).casefold().strip()
    allowed = {"home", "away"}
    if sport == "epl":
        allowed.add("draw")
    if sport in {"nba", "wnba", "mma"}:
        allowed.add("push")
    if outcome not in allowed:
        raise ValueError(f"Unsupported {sport} historical outcome: {outcome}")
    return {
        "sport": sport,
        "provider_sport_key": provider_key,
        "source_event_id": str(row["source_event_id"]),
        "event_time": event_time,
        "source_cutoff": source_cutoff,
        "result_source_key": str(row["result_source_key"]),
        "outcome_role": outcome,
        "features": features,
        "feature_version": feature_version,
        "market_key": spec.internal_market_key,
        "period": spec.period,
    }


def import_history_rows(
    connection: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
    *,
    created_at: datetime,
) -> dict[str, int]:
    ensure_runtime_schema(connection)
    accepted = 0
    rejected = 0
    duplicates = 0
    errors: list[str] = []
    for index, raw in enumerate(rows, 1):
        try:
            row = validate_history_row(dict(raw))
            before = connection.total_changes
            record_training_example(
                connection,
                sport=row["sport"],
                provider_sport_key=row["provider_sport_key"],
                source_event_id=row["source_event_id"],
                event_time=row["event_time"],
                market_key=row["market_key"],
                period=row["period"],
                feature_version=row["feature_version"],
                features=row["features"],
                outcome_role=row["outcome_role"],
                source_cutoff=row["source_cutoff"],
                result_source_key=row["result_source_key"],
                created_at=created_at,
            )
            accepted += 1
        except sqlite3.IntegrityError as exc:
            duplicates += 1
            errors.append(f"row {index}: duplicate/conflict: {exc}")
        except Exception as exc:
            rejected += 1
            errors.append(f"row {index}: {exc}")
    return {
        "accepted": accepted,
        "rejected": rejected,
        "duplicates": duplicates,
        "errors": errors,
    }


def load_json_history(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("Historical JSON must be a list of objects")
    return [dict(row) for row in payload]


def load_csv_history(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if not row.get("features_json"):
                raise ValueError("Historical CSV requires a features_json column")
            rows.append(
                {
                    "sport": row["sport"],
                    "provider_sport_key": row["provider_sport_key"],
                    "source_event_id": row["source_event_id"],
                    "event_time": row["event_time"],
                    "source_cutoff": row["source_cutoff"],
                    "result_source_key": row["result_source_key"],
                    "outcome_role": row["outcome_role"],
                    "feature_version": row.get("feature_version") or FEATURE_VERSION,
                    "features": json.loads(row["features_json"]),
                }
            )
    return rows


def history_status(connection: sqlite3.Connection) -> dict[str, int]:
    ensure_runtime_schema(connection)
    result: dict[str, int] = {}
    for sport in ("nba", "wnba", "epl", "mma", "atp", "wta"):
        feature_version = training_feature_version(sport)
        result[sport] = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM multisport_training_examples
                WHERE sport=? AND feature_version=?
                """,
                (sport, feature_version),
            ).fetchone()[0]
        )
    return result
