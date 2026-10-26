"""Build the approved EPL baseline snapshot from live, timestamped evidence."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3

from .multisport_features import EPL_BASELINE_FEATURES, FEATURE_VERSION
from .multisport_runtime import record_feature


def _latest_official_baseline(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    cutoff: datetime,
) -> tuple[dict[str, float], datetime] | None:
    row = connection.execute(
        """
        SELECT evidence_json, source_updated_at, collected_at
        FROM multisport_research_checks
        WHERE event_id=? AND source_key='premier_league_statistics'
          AND category='statistics_analytics' AND verification_status='verified'
          AND collected_at<=?
        ORDER BY collected_at DESC, research_check_id DESC
        LIMIT 1
        """,
        (event_id, cutoff.isoformat()),
    ).fetchone()
    if row is None:
        return None
    evidence = json.loads(str(row["evidence_json"] or "{}"))
    raw = evidence.get("baseline_features")
    if not isinstance(raw, dict):
        return None
    names = {
        "recent5_points_diff", "home_away_strength_diff",
        "rest_days_diff", "fixture_congestion_diff",
    }
    if set(raw) != names:
        return None
    updated = datetime.fromisoformat(str(row["source_updated_at"] or row["collected_at"]))
    return ({name: float(raw[name]) for name in names}, updated)


def _market_probabilities(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    cutoff: datetime,
) -> tuple[dict[str, float], datetime] | None:
    rows = connection.execute(
        """
        SELECT sportsbook_key, outcome_role, decimal_odds, source_updated_at, collected_at
        FROM multisport_odds_snapshots
        WHERE event_id=? AND market_key='three_way_moneyline' AND period='full_game'
          AND market_status='open' AND source_updated_at<=? AND collected_at<=?
        ORDER BY source_updated_at DESC, odds_snapshot_id DESC
        """,
        (event_id, cutoff.isoformat(), cutoff.isoformat()),
    ).fetchall()
    books: dict[str, dict[str, sqlite3.Row]] = {}
    for row in rows:
        role = str(row["outcome_role"])
        if role not in {"home", "draw", "away"}:
            continue
        books.setdefault(str(row["sportsbook_key"]), {}).setdefault(role, row)
    complete = [quotes for quotes in books.values() if set(quotes) == {"home", "draw", "away"}]
    if not complete:
        return None
    totals = {"home": 0.0, "draw": 0.0, "away": 0.0}
    updated = None
    for quotes in complete:
        raw = {role: 1.0 / float(row["decimal_odds"]) for role, row in quotes.items()}
        overround = sum(raw.values())
        if overround <= 0:
            continue
        for role in totals:
            totals[role] += raw[role] / overround
        candidate = max(datetime.fromisoformat(str(row["source_updated_at"])) for row in quotes.values())
        updated = max(updated, candidate) if updated else candidate
    count = len(complete)
    return ({f"market_{role}_no_vig": totals[role] / count for role in totals}, updated) if updated else None


def record_epl_baseline_live_features(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    cutoff: datetime,
) -> int:
    """Record only the seven features used by the approved EPL baseline model."""
    official = _latest_official_baseline(connection, event_id=event_id, cutoff=cutoff)
    market = _market_probabilities(connection, event_id=event_id, cutoff=cutoff)
    if official is None or market is None:
        return 0
    values = {**market[0], **official[0]}
    if set(values) != set(EPL_BASELINE_FEATURES):
        return 0
    for name in EPL_BASELINE_FEATURES:
        source_key = "the_odds_api" if name.startswith("market_") else "premier_league_statistics"
        source_updated = market[1] if name.startswith("market_") else official[1]
        record_feature(
            connection,
            event_id=event_id,
            sport="epl",
            feature_name=name,
            feature_value=values[name],
            cutoff_time=cutoff,
            feature_version=FEATURE_VERSION,
            collected_at=cutoff,
            source_key=source_key,
            source_updated_at=source_updated,
        )
    return len(values)
