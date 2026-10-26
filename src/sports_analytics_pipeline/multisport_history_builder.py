"""Leakage-safe promotion of stored historical events into training examples.

Closing odds are useful, but they do not satisfy a sport's complete feature
contract.  This module derives only the market features proven by timestamped
pre-event odds, combines them with separately attributed historical evidence,
and records a training example only when every required feature is present.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Iterable

from .multisport_features import (
    EPL_BASELINE_FEATURE_VERSION,
    FEATURE_VERSION,
    required_feature_names,
    training_feature_version,
    validate_feature_payload,
)
from .multisport_runtime import ensure_runtime_schema, record_training_example
from .sports_registry import get_sport_market


UTC = timezone.utc
SUPPORTED_BUILD_SPORTS = ("epl",)
EPL_MARKET_FEATURES = (
    "market_home_no_vig",
    "market_draw_no_vig",
    "market_away_no_vig",
)
EPL_OFFICIAL_VERIFIER_KEY = "football_data_epl_results_archive"


class MarketFeatureError(ValueError):
    """Explain why a stored odds payload could not produce safe market features."""

    def __init__(self, message: str, *, details: dict[str, Any]):
        super().__init__(message)
        self.details = details


def _dt(value: Any, *, label: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a time zone")
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_feature_evidence(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("Historical feature evidence must be a JSON list of objects")
    return [dict(row) for row in payload]


def import_feature_evidence(
    connection: sqlite3.Connection,
    rows: Iterable[dict[str, Any]],
    *,
    collected_at: datetime,
) -> dict[str, Any]:
    """Import timestamped feature evidence without making it training-eligible."""
    accepted = 0
    duplicates = 0
    rejected = 0
    errors: list[str] = []
    for index, raw in enumerate(rows, 1):
        try:
            row = dict(raw)
            required = {
                "provider_key", "source_event_id", "sport", "feature_name",
                "feature_value", "source_key", "source_url", "source_updated_at",
            }
            missing = sorted(required - set(row))
            if missing:
                raise ValueError(f"missing: {', '.join(missing)}")
            sport = str(row["sport"]).casefold().strip()
            if sport not in SUPPORTED_BUILD_SPORTS:
                raise ValueError(f"unsupported history builder sport: {sport}")
            feature_name = str(row["feature_name"]).strip()
            if feature_name not in required_feature_names(sport):
                raise ValueError(f"unapproved {sport} feature: {feature_name}")
            feature_version = str(row.get("feature_version") or FEATURE_VERSION)
            if feature_version != FEATURE_VERSION:
                raise ValueError("unapproved feature version")
            source_url = str(row["source_url"]).strip()
            if not source_url.startswith("https://"):
                raise ValueError("source_url must use HTTPS")
            provider_key = str(row["provider_key"]).strip()
            source_event_id = str(row["source_event_id"]).strip()
            event = connection.execute(
                """
                SELECT event_time FROM multisport_historical_events
                WHERE provider_key=? AND source_event_id=? AND sport=?
                """,
                (provider_key, source_event_id, sport),
            ).fetchone()
            if event is None:
                raise ValueError("matching stored historical event was not found")
            event_time = _dt(event["event_time"], label="event_time")
            source_updated_at = _dt(row["source_updated_at"], label="source_updated_at")
            if source_updated_at >= event_time:
                raise ValueError("feature evidence must be updated strictly before event time")
            feature_value = float(row["feature_value"])
            if feature_value != feature_value or feature_value in {float("inf"), float("-inf")}:
                raise ValueError("feature_value must be finite")
            evidence = row.get("evidence", {})
            if not isinstance(evidence, dict):
                raise ValueError("evidence must be an object")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO multisport_historical_feature_evidence (
                    provider_key, source_event_id, sport, feature_name, feature_value,
                    feature_version, source_key, source_url, source_updated_at,
                    collected_at, evidence_json, test_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    provider_key, source_event_id, sport, feature_name, feature_value,
                    feature_version, str(row["source_key"]).strip(), source_url,
                    _iso(source_updated_at), _iso(collected_at), _canonical(evidence),
                ),
            )
            if cursor.rowcount:
                accepted += 1
            else:
                duplicates += 1
        except Exception as exc:
            rejected += 1
            errors.append(f"row {index}: {exc}")
    connection.commit()
    return {
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
        "errors": errors,
        "training_examples_created": 0,
        "test_only": True,
    }


def _positive_decimal(value: Any) -> float:
    price = float(value)
    if price <= 1.0:
        raise ValueError("decimal odds must be greater than 1.0")
    return price


def epl_market_features(
    payload: dict[str, Any],
    *,
    event_time: datetime,
) -> tuple[dict[str, float], datetime, dict[str, Any]]:
    """Return median-book, normalized 1X2 probabilities from pre-event quotes."""
    if not isinstance(payload, dict):
        raise TypeError("historical odds payload must be an object")
    bookmakers = payload.get("bookmakers")
    if not isinstance(bookmakers, dict):
        raise ValueError("historical odds payload is missing bookmakers")
    probabilities: list[tuple[float, float, float]] = []
    quote_times: list[datetime] = []
    used_books: list[str] = []
    rejection_reasons: Counter[str] = Counter()
    for bookmaker, markets in sorted(bookmakers.items()):
        if not isinstance(markets, list):
            rejection_reasons["invalid_bookmaker_market_list"] += 1
            continue
        accepted_for_book = False
        moneyline_seen = False
        for market in markets:
            if not isinstance(market, dict) or str(market.get("name", "")).upper() != "ML":
                continue
            moneyline_seen = True
            try:
                updated_at = _dt(market.get("updatedAt"), label="odds updatedAt")
            except (TypeError, ValueError):
                rejection_reasons["invalid_or_missing_quote_timestamp"] += 1
                continue
            if updated_at >= event_time:
                rejection_reasons["quote_at_or_after_event_time"] += 1
                continue
            rows = market.get("odds")
            if not isinstance(rows, list):
                rejection_reasons["invalid_odds_list"] += 1
                continue
            complete = next(
                (
                    row for row in rows
                    if isinstance(row, dict) and {"home", "draw", "away"} <= set(row)
                ),
                None,
            )
            if complete is None:
                rejection_reasons["complete_home_draw_away_prices_missing"] += 1
                continue
            try:
                implied = (
                    1.0 / _positive_decimal(complete["home"]),
                    1.0 / _positive_decimal(complete["draw"]),
                    1.0 / _positive_decimal(complete["away"]),
                )
            except (TypeError, ValueError):
                rejection_reasons["invalid_decimal_prices"] += 1
                continue
            overround = sum(implied)
            probabilities.append(tuple(value / overround for value in implied))
            quote_times.append(updated_at)
            used_books.append(str(bookmaker))
            accepted_for_book = True
            break
        if not accepted_for_book and not moneyline_seen:
            rejection_reasons["moneyline_market_missing"] += 1
    if not probabilities:
        if not bookmakers:
            rejection_reasons["bookmaker_quotes_missing"] += 1
        details = {
            "bookmakers": [],
            "accepted_quotes": 0,
            "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
        }
        raise MarketFeatureError(
            "no complete pre-event EPL 1X2 quote was available",
            details=details,
        )
    consensus = [median(row[index] for row in probabilities) for index in range(3)]
    consensus_total = sum(consensus)
    values = {
        name: consensus[index] / consensus_total
        for index, name in enumerate(EPL_MARKET_FEATURES)
    }
    return values, max(quote_times), {
        "bookmakers": used_books,
        "accepted_quotes": len(probabilities),
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
    }


def _latest_evidence(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    source_event_id: str,
    sport: str,
    event_time: datetime,
) -> tuple[dict[str, float], dict[str, datetime], dict[str, str]]:
    rows = connection.execute(
        """
        SELECT feature_name, feature_value, source_updated_at, source_key
        FROM multisport_historical_feature_evidence
        WHERE provider_key=? AND source_event_id=? AND sport=?
          AND feature_version=? AND source_updated_at < ?
        ORDER BY source_updated_at DESC, historical_feature_evidence_id DESC
        """,
        (provider_key, source_event_id, sport, FEATURE_VERSION, _iso(event_time)),
    ).fetchall()
    values: dict[str, float] = {}
    updates: dict[str, datetime] = {}
    sources: dict[str, str] = {}
    for row in rows:
        name = str(row["feature_name"])
        if name in values:
            continue
        values[name] = float(row["feature_value"])
        updates[name] = _dt(row["source_updated_at"], label="source_updated_at")
        sources[name] = str(row["source_key"])
    return values, updates, sources


def _record_audit(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    source_event_id: str,
    sport: str,
    feature_version: str,
    status: str,
    source_cutoff: datetime | None,
    available: list[str],
    missing: list[str],
    details: dict[str, Any],
    attempted_at: datetime,
) -> None:
    connection.execute(
        """
        INSERT INTO multisport_history_example_audits (
            provider_key, source_event_id, sport, feature_version, status,
            source_cutoff, available_features_json, missing_features_json,
            details_json, first_attempted_at, last_attempted_at, attempt_count, test_only
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
        ON CONFLICT(provider_key, source_event_id, sport, feature_version) DO UPDATE SET
            status=excluded.status,
            source_cutoff=excluded.source_cutoff,
            available_features_json=excluded.available_features_json,
            missing_features_json=excluded.missing_features_json,
            details_json=excluded.details_json,
            last_attempted_at=excluded.last_attempted_at,
            attempt_count=multisport_history_example_audits.attempt_count + 1,
            test_only=1
        """,
        (
            provider_key, source_event_id, sport, feature_version, status,
            _iso(source_cutoff) if source_cutoff else None,
            _canonical(available), _canonical(missing), _canonical(details),
            _iso(attempted_at), _iso(attempted_at),
        ),
    )


def build_history_examples(
    connection: sqlite3.Connection,
    *,
    sports: tuple[str, ...] = ("epl",),
    built_at: datetime,
    minimum_examples: int = 50,
) -> dict[str, Any]:
    """Build only complete, pre-event examples and report every blocker."""
    unsupported = sorted(set(sports) - set(SUPPORTED_BUILD_SPORTS))
    if unsupported or not sports:
        raise ValueError(f"Unsupported or empty history builder sports: {', '.join(unsupported)}")
    if len(set(sports)) != len(sports):
        raise ValueError("History builder sports cannot contain duplicates")
    if minimum_examples < 1:
        raise ValueError("minimum_examples must be positive")
    ensure_runtime_schema(connection)
    result: dict[str, Any] = {"sports": {}, "test_only": True}
    for sport in sports:
        example_feature_version = training_feature_version(sport)
        required = set(
            required_feature_names(
                sport,
                feature_version=example_feature_version,
            )
        )
        feature_coverage: Counter[str] = Counter()
        missing_patterns: Counter[str] = Counter()
        market_statuses: Counter[str] = Counter()
        market_rejection_reasons: Counter[str] = Counter()
        market_samples: dict[str, list[str]] = {}
        scanned = created = blocked = existing = 0
        records_seen = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM multisport_historical_events
                WHERE sport=? AND status='settled'
                  AND result_role IN ('home','away','draw')
                """,
                (sport,),
            ).fetchone()[0]
        )
        events = connection.execute(
            """
            SELECT event.* FROM multisport_historical_events AS event
            JOIN multisport_historical_event_verifications AS verification
              ON verification.provider_key=event.provider_key
             AND verification.source_event_id=event.source_event_id
             AND verification.sport=event.sport
            WHERE event.sport=? AND event.status='settled'
              AND event.result_role IN ('home','away','draw')
              AND verification.verifier_key=?
              AND verification.verification_status='verified'
            ORDER BY event.event_time, event.historical_event_id
            """,
            (sport, EPL_OFFICIAL_VERIFIER_KEY),
        ).fetchall()
        quarantined_unverified = records_seen - len(events)
        for event in events:
            scanned += 1
            provider_key = str(event["provider_key"])
            source_event_id = str(event["source_event_id"])
            event_time = _dt(event["event_time"], label="event_time")
            spec = get_sport_market(sport)
            provider_sport_key = spec.exact_provider_sport_keys[0]
            prior = connection.execute(
                """
                SELECT features_json, source_cutoff FROM multisport_training_examples
                WHERE sport=? AND provider_sport_key=? AND source_event_id=?
                  AND market_key=? AND period=? AND feature_version=?
                """,
                (
                    sport, provider_sport_key, source_event_id,
                    spec.internal_market_key, spec.period, example_feature_version,
                ),
            ).fetchone()
            if prior:
                existing += 1
                market_statuses["existing_training_example"] += 1
                stored_features = json.loads(str(prior["features_json"]))
                stored_available = sorted(required & set(stored_features))
                for name in stored_available:
                    feature_coverage[name] += 1
                _record_audit(
                    connection, provider_key=provider_key, source_event_id=source_event_id,
                    sport=sport, feature_version=example_feature_version, status="existing",
                    source_cutoff=_dt(prior["source_cutoff"], label="source_cutoff"),
                    available=stored_available, missing=[], details={}, attempted_at=built_at,
                )
                continue

            values, updates, sources = _latest_evidence(
                connection,
                provider_key=provider_key,
                source_event_id=source_event_id,
                sport=sport,
                event_time=event_time,
            )
            details: dict[str, Any] = {"evidence_sources": sources}
            odds_payload = event["odds_payload_json"]
            if odds_payload:
                try:
                    market_values, market_cutoff, market_details = epl_market_features(
                        json.loads(str(odds_payload)), event_time=event_time,
                    )
                    values.update(market_values)
                    updates.update({name: market_cutoff for name in market_values})
                    details["market"] = market_details
                    market_statuses["usable_pre_event_1x2"] += 1
                    for reason, count in market_details["rejection_reason_counts"].items():
                        market_rejection_reasons[reason] += int(count)
                except MarketFeatureError as exc:
                    details["market_error"] = str(exc)
                    details["market"] = exc.details
                    market_statuses["no_complete_pre_event_1x2"] += 1
                    market_samples.setdefault("no_complete_pre_event_1x2", [])
                    if len(market_samples["no_complete_pre_event_1x2"]) < 5:
                        market_samples["no_complete_pre_event_1x2"].append(source_event_id)
                    for reason, count in exc.details["rejection_reason_counts"].items():
                        market_rejection_reasons[reason] += int(count)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    details["market_error"] = str(exc)
                    market_statuses["malformed_odds_payload"] += 1
                    market_samples.setdefault("malformed_odds_payload", [])
                    if len(market_samples["malformed_odds_payload"]) < 5:
                        market_samples["malformed_odds_payload"].append(source_event_id)
            else:
                details["market_error"] = "stored odds payload is missing"
                market_statuses["missing_odds_payload"] += 1
                market_samples.setdefault("missing_odds_payload", [])
                if len(market_samples["missing_odds_payload"]) < 5:
                    market_samples["missing_odds_payload"].append(source_event_id)
            available = sorted(required & set(values))
            missing = sorted(required - set(values))
            for name in available:
                feature_coverage[name] += 1
            source_cutoff = max((updates[name] for name in available), default=None)
            if missing:
                blocked += 1
                missing_patterns[", ".join(missing)] += 1
                _record_audit(
                    connection, provider_key=provider_key, source_event_id=source_event_id,
                    sport=sport, feature_version=example_feature_version,
                    status="blocked", source_cutoff=source_cutoff,
                    available=available, missing=missing, details=details, attempted_at=built_at,
                )
                continue
            baseline_values = {name: values[name] for name in required}
            normalized = validate_feature_payload(
                sport,
                baseline_values,
                feature_version=example_feature_version,
            )
            if source_cutoff is None or source_cutoff >= event_time:
                raise ValueError("Complete historical feature vector lacks a safe pre-event cutoff")
            record_training_example(
                connection,
                sport=sport,
                provider_sport_key=provider_sport_key,
                source_event_id=source_event_id,
                event_time=event_time,
                market_key=spec.internal_market_key,
                period=spec.period,
                feature_version=example_feature_version,
                features=normalized,
                outcome_role=str(event["result_role"]),
                source_cutoff=source_cutoff,
                result_source_key=provider_key,
                created_at=built_at,
            )
            created += 1
            _record_audit(
                connection, provider_key=provider_key, source_event_id=source_event_id,
                sport=sport, feature_version=example_feature_version,
                status="created", source_cutoff=source_cutoff,
                available=available, missing=[], details=details, attempted_at=built_at,
            )
        connection.commit()
        total = int(connection.execute(
            """
            SELECT COUNT(*) FROM multisport_training_examples
            WHERE sport=? AND feature_version=?
            """,
            (sport, example_feature_version),
        ).fetchone()[0])
        result["sports"][sport] = {
            "records_seen": records_seen,
            "scanned": scanned,
            "quarantined_unverified": quarantined_unverified,
            "created": created,
            "existing": existing,
            "blocked": blocked,
            "feature_coverage": {
                name: int(feature_coverage[name]) for name in sorted(required)
            },
            "missing_feature_patterns": dict(sorted(missing_patterns.items())),
            "market_diagnostics": {
                "status_counts": dict(sorted(market_statuses.items())),
                "rejection_reason_counts": dict(sorted(market_rejection_reasons.items())),
                "sample_event_ids_by_status": dict(sorted(market_samples.items())),
            },
            "training_examples_total": total,
            "training_minimum": minimum_examples,
            "examples_needed": max(0, minimum_examples - total),
            "training_ready": total >= minimum_examples,
            "feature_version": example_feature_version,
            "enhanced_features_deferred": sorted(
                set(required_feature_names(sport)) - required
            ) if example_feature_version == EPL_BASELINE_FEATURE_VERSION else [],
        }
    result["all_selected_sports_ready"] = all(
        row["training_ready"] for row in result["sports"].values()
    )
    return result
