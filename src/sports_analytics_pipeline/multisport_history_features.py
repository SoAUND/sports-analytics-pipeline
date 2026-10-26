"""Leakage-safe historical feature promotion for NBA, WNBA, MMA, ATP and WTA.

This module never infers unavailable pre-event features from final results.  A row is
eligible only when the event result has an approved independent verification, every
required feature has timestamped evidence strictly before event start, and the target
outcome matches the controlled market.  Basketball spread targets remain fail-closed
until an exact historical spread/cover outcome is independently verified.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any, Iterable

from .multisport_features import FEATURE_VERSION, required_feature_names, validate_feature_payload
from .multisport_runtime import ensure_runtime_schema, record_training_example
from .sports_registry import get_sport_market

UTC = timezone.utc
SUPPORTED_SPORTS = ("nba", "wnba", "mma", "atp", "wta")
BINARY_MONEYLINE_SPORTS = frozenset({"mma", "atp", "wta"})
BASKETBALL_SPREAD_SPORTS = frozenset({"nba", "wnba"})
APPROVED_RESULT_VERIFIERS = {
    "nba": frozenset({"official_nba_results_archive"}),
    "wnba": frozenset({"official_wnba_results_archive"}),
    "mma": frozenset({"official_ufc_results_archive"}),
    "atp": frozenset({"official_atp_results_archive"}),
    "wta": frozenset({"official_wta_results_archive"}),
}


class HistoricalFeatureError(ValueError):
    """A safe training example cannot be constructed from the available evidence."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


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
    """Persist attributed pre-event evidence without automatically creating examples."""
    ensure_runtime_schema(connection)
    accepted = duplicates = rejected = 0
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
            if sport not in SUPPORTED_SPORTS:
                raise ValueError(f"unsupported non-EPL history sport: {sport}")
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
                """SELECT event_time FROM multisport_historical_events
                   WHERE provider_key=? AND source_event_id=? AND sport=?""",
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
                """INSERT OR IGNORE INTO multisport_historical_feature_evidence (
                    provider_key, source_event_id, sport, feature_name, feature_value,
                    feature_version, source_key, source_url, source_updated_at,
                    collected_at, evidence_json, test_only
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
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


def binary_moneyline_market_feature(
    payload: dict[str, Any],
    *,
    event_time: datetime,
) -> tuple[float, datetime, dict[str, Any]]:
    """Return median-book no-vig first-listed probability from safe pre-event quotes."""
    if not isinstance(payload, dict):
        raise TypeError("historical odds payload must be an object")
    bookmakers = payload.get("bookmakers")
    if not isinstance(bookmakers, dict):
        raise HistoricalFeatureError("historical odds payload is missing bookmakers")
    probabilities: list[float] = []
    quote_times: list[datetime] = []
    used_books: list[str] = []
    rejection_reasons: Counter[str] = Counter()
    for bookmaker, markets in sorted(bookmakers.items()):
        if not isinstance(markets, list):
            rejection_reasons["invalid_bookmaker_market_list"] += 1
            continue
        moneyline_seen = False
        accepted = False
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
                (row for row in rows if isinstance(row, dict) and {"home", "away"} <= set(row)),
                None,
            )
            if complete is None:
                rejection_reasons["complete_home_away_prices_missing"] += 1
                continue
            try:
                home = 1.0 / _positive_decimal(complete["home"])
                away = 1.0 / _positive_decimal(complete["away"])
            except (TypeError, ValueError):
                rejection_reasons["invalid_decimal_prices"] += 1
                continue
            probabilities.append(home / (home + away))
            quote_times.append(updated_at)
            used_books.append(str(bookmaker))
            accepted = True
            break
        if not accepted and not moneyline_seen:
            rejection_reasons["moneyline_market_missing"] += 1
    details = {
        "bookmakers": used_books,
        "accepted_quotes": len(probabilities),
        "rejection_reason_counts": dict(sorted(rejection_reasons.items())),
    }
    if not probabilities:
        raise HistoricalFeatureError(
            "no complete pre-event two-way moneyline quote was available",
            details=details,
        )
    return median(probabilities), max(quote_times), details


def _latest_evidence(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    source_event_id: str,
    sport: str,
    event_time: datetime,
) -> tuple[dict[str, float], dict[str, datetime], dict[str, str]]:
    rows = connection.execute(
        """SELECT feature_name, feature_value, source_updated_at, source_key
           FROM multisport_historical_feature_evidence
           WHERE provider_key=? AND source_event_id=? AND sport=?
             AND feature_version=? AND source_updated_at < ?
           ORDER BY source_updated_at DESC, historical_feature_evidence_id DESC""",
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


def _approved_result_verified(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    source_event_id: str,
    sport: str,
) -> bool:
    allowed = APPROVED_RESULT_VERIFIERS[sport]
    rows = connection.execute(
        """SELECT verifier_key FROM multisport_historical_event_verifications
           WHERE provider_key=? AND source_event_id=? AND sport=?
             AND verification_status='verified'""",
        (provider_key, source_event_id, sport),
    ).fetchall()
    return any(str(row["verifier_key"]) in allowed for row in rows)


def _record_audit(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    source_event_id: str,
    sport: str,
    status: str,
    source_cutoff: datetime | None,
    available: list[str],
    missing: list[str],
    details: dict[str, Any],
    attempted_at: datetime,
) -> None:
    connection.execute(
        """INSERT INTO multisport_history_example_audits (
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
            test_only=1""",
        (
            provider_key, source_event_id, sport, FEATURE_VERSION, status,
            _iso(source_cutoff) if source_cutoff else None,
            _canonical(available), _canonical(missing), _canonical(details),
            _iso(attempted_at), _iso(attempted_at),
        ),
    )


def build_history_examples(
    connection: sqlite3.Connection,
    *,
    sports: tuple[str, ...] = SUPPORTED_SPORTS,
    built_at: datetime,
    minimum_examples: int = 50,
) -> dict[str, Any]:
    """Promote only independently verified, complete, pre-event feature vectors."""
    unsupported = sorted(set(sports) - set(SUPPORTED_SPORTS))
    if unsupported or not sports:
        raise ValueError(f"Unsupported or empty history feature sports: {', '.join(unsupported)}")
    if len(set(sports)) != len(sports):
        raise ValueError("History feature sports cannot contain duplicates")
    if minimum_examples < 1:
        raise ValueError("minimum_examples must be positive")
    ensure_runtime_schema(connection)
    report: dict[str, Any] = {"sports": {}, "test_only": True}
    for sport in sports:
        required = set(required_feature_names(sport))
        records = connection.execute(
            """SELECT * FROM multisport_historical_events
               WHERE sport=? AND status='settled' AND result_role IN ('home','away')
               ORDER BY event_time, historical_event_id""",
            (sport,),
        ).fetchall()
        scanned = verified = created = existing = blocked = quarantined = 0
        missing_patterns: Counter[str] = Counter()
        feature_coverage: Counter[str] = Counter()
        blocker_counts: Counter[str] = Counter()
        for event in records:
            provider_key = str(event["provider_key"])
            source_event_id = str(event["source_event_id"])
            event_time = _dt(event["event_time"], label="event_time")
            if not _approved_result_verified(
                connection,
                provider_key=provider_key,
                source_event_id=source_event_id,
                sport=sport,
            ):
                quarantined += 1
                blocker_counts["approved_result_verification_missing"] += 1
                continue
            verified += 1
            scanned += 1
            spec = get_sport_market(sport)
            if spec.exact_provider_sport_keys:
                provider_sport_key = spec.exact_provider_sport_keys[0]
            elif spec.provider_sport_key_prefixes:
                # Historical Odds-API.io tennis records do not carry The Odds API's
                # tournament-specific key. Use a controlled tour-family history key
                # rather than inventing a tournament identity from another provider.
                provider_sport_key = f"{spec.provider_sport_key_prefixes[0]}historical"
            else:
                raise HistoricalFeatureError(f"No controlled provider key family for {sport}")
            prior = connection.execute(
                """SELECT source_cutoff FROM multisport_training_examples
                   WHERE sport=? AND provider_sport_key=? AND source_event_id=?
                     AND market_key=? AND period=? AND feature_version=?""",
                (
                    sport, provider_sport_key, source_event_id,
                    spec.internal_market_key, spec.period, FEATURE_VERSION,
                ),
            ).fetchone()
            if prior:
                existing += 1
                continue
            values, updates, sources = _latest_evidence(
                connection,
                provider_key=provider_key,
                source_event_id=source_event_id,
                sport=sport,
                event_time=event_time,
            )
            details: dict[str, Any] = {"evidence_sources": sources}
            if sport in BINARY_MONEYLINE_SPORTS and "market_home_no_vig" not in values:
                payload = event["odds_payload_json"]
                if payload:
                    try:
                        probability, cutoff, market_details = binary_moneyline_market_feature(
                            json.loads(str(payload)), event_time=event_time,
                        )
                        values["market_home_no_vig"] = probability
                        updates["market_home_no_vig"] = cutoff
                        details["market"] = market_details
                    except (HistoricalFeatureError, json.JSONDecodeError, TypeError, ValueError) as exc:
                        details["market_error"] = str(exc)
                        if isinstance(exc, HistoricalFeatureError):
                            details["market"] = exc.details
            available = sorted(required & set(values))
            for name in available:
                feature_coverage[name] += 1
            source_cutoff = max((updates[name] for name in available), default=None)
            missing = sorted(required - set(values))
            if sport in BASKETBALL_SPREAD_SPORTS:
                blocked += 1
                blocker_counts["spread_cover_outcome_verification_required"] += 1
                _record_audit(
                    connection,
                    provider_key=provider_key,
                    source_event_id=source_event_id,
                    sport=sport,
                    status="blocked",
                    source_cutoff=source_cutoff,
                    available=available,
                    missing=missing,
                    details={**details, "target_blocker": "final game winner is not a spread-cover outcome"},
                    attempted_at=built_at,
                )
                continue
            if missing:
                blocked += 1
                missing_patterns[", ".join(missing)] += 1
                blocker_counts["required_pre_event_features_missing"] += 1
                _record_audit(
                    connection,
                    provider_key=provider_key,
                    source_event_id=source_event_id,
                    sport=sport,
                    status="blocked",
                    source_cutoff=source_cutoff,
                    available=available,
                    missing=missing,
                    details=details,
                    attempted_at=built_at,
                )
                continue
            if source_cutoff is None or source_cutoff >= event_time:
                raise ValueError("Complete historical feature vector lacks a safe pre-event cutoff")
            normalized = validate_feature_payload(sport, {name: values[name] for name in required})
            record_training_example(
                connection,
                sport=sport,
                provider_sport_key=provider_sport_key,
                source_event_id=source_event_id,
                event_time=event_time,
                market_key=spec.internal_market_key,
                period=spec.period,
                feature_version=FEATURE_VERSION,
                features=normalized,
                outcome_role=str(event["result_role"]),
                source_cutoff=source_cutoff,
                result_source_key=next(iter(APPROVED_RESULT_VERIFIERS[sport])),
                created_at=built_at,
            )
            created += 1
            _record_audit(
                connection,
                provider_key=provider_key,
                source_event_id=source_event_id,
                sport=sport,
                status="created",
                source_cutoff=source_cutoff,
                available=available,
                missing=[],
                details=details,
                attempted_at=built_at,
            )
        connection.commit()
        total = int(connection.execute(
            "SELECT COUNT(*) FROM multisport_training_examples WHERE sport=? AND feature_version=?",
            (sport, FEATURE_VERSION),
        ).fetchone()[0])
        report["sports"][sport] = {
            "records_seen": len(records),
            "verified_records": verified,
            "scanned": scanned,
            "quarantined_unverified": quarantined,
            "created": created,
            "existing": existing,
            "blocked": blocked,
            "blocker_counts": dict(sorted(blocker_counts.items())),
            "feature_coverage": {name: int(feature_coverage[name]) for name in sorted(required)},
            "missing_feature_patterns": dict(sorted(missing_patterns.items())),
            "training_examples_total": total,
            "training_minimum": minimum_examples,
            "examples_needed": max(0, minimum_examples - total),
            "training_ready": total >= minimum_examples,
            "feature_version": FEATURE_VERSION,
        }
    report["all_selected_sports_ready"] = all(
        row["training_ready"] for row in report["sports"].values()
    )
    return report
