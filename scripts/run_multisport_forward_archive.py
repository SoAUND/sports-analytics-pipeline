#!/usr/bin/env python3
"""Archive fresh pregame odds without evaluating picks or counting OOS."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.multisport_odds import MultiSportOddsCollector
from sports_analytics_pipeline.collectors.multisport_scores import AUTO_SCORE_SPORTS, MultiSportScoresCollector
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.multisport_operations import ALL_SPORTS, bootstrap, collect_sport
from sports_analytics_pipeline.multisport_score_storage import persist_score_batch
from sports_analytics_pipeline.sports_registry import get_sport_market

UTC = timezone.utc
SCORE_GRACE_MINUTES = {"nba": 180, "wnba": 180, "epl": 150}
ARCHIVE_MODES = {'research', 'test_only'}

def _sports(value: str) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(x.strip().casefold() for x in value.split(',') if x.strip()))
    unknown = tuple(x for x in selected if x not in ALL_SPORTS)
    if unknown:
        raise argparse.ArgumentTypeError(f"Unsupported sports: {','.join(unknown)}")
    return selected

def _require_archive_mode(mode: str) -> None:
    if mode not in ARCHIVE_MODES:
        raise SystemExit('Unsupported research mode')

def _archived_result_due(connection: sqlite3.Connection, *, sport: str, as_of: datetime) -> bool:
    if sport not in SCORE_GRACE_MINUTES:
        return False
    cutoff = as_of.astimezone(UTC) - timedelta(minutes=SCORE_GRACE_MINUTES[sport])
    row = connection.execute(
        """SELECT 1 FROM multisport_events e
           JOIN multisport_odds_snapshots o ON o.event_id=e.event_id
           LEFT JOIN multisport_results r ON r.event_id=e.event_id
           WHERE e.sport=? AND r.event_id IS NULL
             AND o.collected_at < e.commence_time AND e.commence_time<=? LIMIT 1""",
        (sport, cutoff.isoformat()),
    ).fetchone()
    return row is not None

def _collect_archived_results(connection: sqlite3.Connection, *, settings: Settings, sport: str, as_of: datetime) -> dict[str, object]:
    if sport not in AUTO_SCORE_SPORTS:
        return {"sport": sport, "stored": 0, "note": "result connector pending; archived odds retained"}
    if not _archived_result_due(connection, sport=sport, as_of=as_of):
        return {"sport": sport, "stored": 0, "note": "no archived event due for score polling"}
    transport = UrllibTransport(timeout_seconds=settings.http_timeout_seconds, attempts=settings.http_retry_attempts)
    collector = MultiSportScoresCollector(api_key=settings.require_odds_api_key(), transport=transport)
    stored = skipped = 0
    errors: list[str] = []
    for provider_key in get_sport_market(sport).exact_provider_sport_keys:
        try:
            result = persist_score_batch(connection, collector.collect(sport=sport, provider_sport_key=provider_key))
            stored += int(result["stored"])
            skipped += int(result["skipped"])
            errors.extend(result.get("errors", ()))
        except Exception as exc:
            errors.append(f"{provider_key}: {exc}")
    connection.commit()
    return {"sport": sport, "stored": stored, "skipped": skipped, "errors": tuple(errors)}

def _status(connection: sqlite3.Connection, sports: tuple[str, ...]) -> dict[str, object]:
    marks = ','.join('?' for _ in sports)
    rows = connection.execute(
        f"""SELECT e.sport, COUNT(DISTINCT e.event_id) events_with_pregame_odds,
            COUNT(o.odds_snapshot_id) pregame_quotes, MIN(o.collected_at) first_snapshot,
            MAX(o.collected_at) latest_snapshot
            FROM multisport_events e JOIN multisport_odds_snapshots o ON o.event_id=e.event_id
            WHERE e.sport IN ({marks}) AND o.collected_at < e.commence_time
            GROUP BY e.sport ORDER BY e.sport""", sports).fetchall()
    return {str(r["sport"]): {"events_with_pregame_odds": int(r["events_with_pregame_odds"]),
            "pregame_quotes": int(r["pregame_quotes"]), "first_snapshot": r["first_snapshot"],
            "latest_snapshot": r["latest_snapshot"]} for r in rows}

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--sports', type=_sports, default=ALL_SPORTS)
    parser.add_argument('--env', default='.env')
    args = parser.parse_args()
    settings = Settings.load(args.env)
    _require_archive_mode(settings.bot_mode)
    now = datetime.now(UTC)
    db = sqlite3.connect(settings.database_path)
    db.row_factory = sqlite3.Row
    try:
        bootstrap(db, as_of=now)
        transport = UrllibTransport(timeout_seconds=settings.http_timeout_seconds, attempts=settings.http_retry_attempts)
        collector = MultiSportOddsCollector(api_key=settings.require_odds_api_key(), transport=transport,
            bookmakers=settings.market_consensus_bookmakers or settings.accessible_bookmakers, regions=('us',))
        catalog = collector.collect_sports_catalog()
        collected = {}
        results = {}
        for sport in args.sports:
            collected[sport] = collect_sport(db, settings=settings, sport=sport, as_of=now,
                collector=collector, catalog=catalog, transport=transport)
            results[sport] = _collect_archived_results(db, settings=settings, sport=sport, as_of=now)
        print(json.dumps({"status":"complete","mode":"test_only_forward_history","automatic_wagering":False,
            "paper_evaluation":False,"countable_oos":False,"as_of":now.isoformat(),"sports":args.sports,
            "collection":collected,"results":results,"archive":_status(db,args.sports)}, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        db.close()

if __name__ == '__main__':
    raise SystemExit(main())
