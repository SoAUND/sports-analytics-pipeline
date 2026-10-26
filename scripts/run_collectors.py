#!/usr/bin/env python3
"""Run our Phase 2 collectors without issuing picks or taking real-money actions."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.collectors.base import CollectionBatch, MappingTeamResolver
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.nflverse import MappingStadiumResolver, NflverseCollector
from sports_analytics_pipeline.collectors.nws import NwsCollector
from sports_analytics_pipeline.collectors.official_nfl import OfficialNflCollector
from sports_analytics_pipeline.collectors.official_schedule import load_bundled_preseason_schedule
from sports_analytics_pipeline.collectors.storage import CollectorRepository, iso
from sports_analytics_pipeline.collectors.the_odds_api import (
    PRESEASON_SPORT_KEY,
    SPORT_KEY,
    TheOddsApiCollector,
    parse_current_spreads,
    parse_timestamp,
)
from sports_analytics_pipeline.collectors.therundown import TheRundownCollector
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import (
    alias_mapping,
    seed_reference_data,
    stadium_alias_mapping,
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _settings(args) -> Settings:
    return Settings.load(args.env)


def _open(settings: Settings):
    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db)
    return db


def _transport(settings: Settings) -> UrllibTransport:
    return UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )


def _complete_run(repo, run_id, batch, stored, warnings=(), status=None):
    all_warnings = tuple(getattr(batch, "warnings", ())) + tuple(warnings)
    repo.finish_run(
        run_id,
        finished_at=now(),
        status=status or ("partial" if all_warnings else "succeeded"),
        received=len(getattr(batch, "records", getattr(batch, "quotes", ()))),
        stored=stored,
        warnings=all_warnings,
        metadata=getattr(batch, "metadata", {}),
    )


def _fail_run(repo, run_id, exc: Exception):
    repo.finish_run(
        run_id,
        finished_at=now(),
        status="failed",
        error_message=str(exc),
    )


def collect_nflverse(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    collector = NflverseCollector(
        transport=_transport(settings),
        team_resolver=MappingTeamResolver(alias_mapping(db)),
        stadium_resolver=MappingStadiumResolver(stadium_alias_mapping(db)),
    )
    results: dict[str, object] = {}
    operations = [
        ("schedules", collector.collect_schedules, repo.persist_schedules),
        ("rosters", collector.collect_rosters, repo.persist_rosters),
        ("injuries", collector.collect_injuries, repo.persist_injuries),
    ]
    for name, collect, persist in operations:
        run_id = repo.begin_run("nflverse", f"{name}_{args.season}", now())
        try:
            batch = collect(args.season)
            stored = persist(batch)
            _complete_run(repo, run_id, batch, stored)
            results[name] = {"received": len(batch.records), "stored": stored, "warnings": len(batch.warnings)}
        except Exception as exc:
            _fail_run(repo, run_id, exc)
            results[name] = {"status": "failed", "error": str(exc)}

    if args.include_pbp:
        run_id = repo.begin_run("nflverse", f"play_by_play_{args.season}", now())
        try:
            archive = collector.collect_play_by_play_archive(args.season)
            path = repo.persist_archive(archive, settings.database_path.parent / "raw")
            repo.finish_run(run_id, finished_at=now(), status="succeeded", received=1, stored=1)
            results["play_by_play"] = {"stored": 1, "path": str(path)}
        except Exception as exc:
            _fail_run(repo, run_id, exc)
            results["play_by_play"] = {"status": "failed", "error": str(exc)}
    db.close()
    return results


def collect_preseason(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    run_id = repo.begin_run("nfl_schedule", f"preseason_{args.season}", now())
    try:
        batch = load_bundled_preseason_schedule(args.season, db, collected_at=now())
        stored = repo.persist_official_schedules(batch)
        _complete_run(repo, run_id, batch, stored)
        return {
            "season": args.season,
            "received": len(batch.records),
            "stored": stored,
            "weeks": {"hall_of_fame": 1, "week_1": 16, "week_2": 16, "week_3": 16},
        }
    except Exception as exc:
        _fail_run(repo, run_id, exc)
        raise
    finally:
        db.close()


def _odds_collector(settings, db, *, sport_key=SPORT_KEY) -> TheOddsApiCollector:
    configured_books = tuple(
        dict.fromkeys(
            settings.market_consensus_bookmakers + settings.accessible_bookmakers
        )
    )
    return TheOddsApiCollector(
        api_key=settings.require_odds_api_key(),
        resolver=MappingTeamResolver(alias_mapping(db)),
        transport=_transport(settings),
        bookmakers=configured_books,
        regions=() if configured_books else ("us",),
        sport_key=sport_key,
    )


def collect_odds(args, historical=False, preseason=False) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    sport_key = PRESEASON_SPORT_KEY if preseason else SPORT_KEY
    endpoint = (
        "historical_odds"
        if historical
        else "current_preseason_odds" if preseason else "current_odds"
    )
    run_id = repo.begin_run("the_odds_api", endpoint, now())
    try:
        collector = _odds_collector(settings, db, sport_key=sport_key)
        if historical:
            requested_at = datetime.fromisoformat(args.at.replace("Z", "+00:00"))
            batch = collector.collect_historical_spreads(requested_at)
        else:
            batch = collector.collect_current_spreads()
        stored, warnings = repo.persist_odds(
            batch,
            accessible_bookmakers=settings.accessible_bookmakers,
            consensus_bookmakers=settings.market_consensus_bookmakers,
            stale_after_minutes=settings.odds_stale_after_minutes,
        )
        _complete_run(repo, run_id, batch, stored, warnings)
        result = {
            "received": len(batch.quotes),
            "stored": stored,
            "warnings": list(warnings),
            "quota": {
                key: batch.metadata.get(key)
                for key in ("requests_remaining", "requests_used", "request_cost")
            },
        }
    except Exception as exc:
        _fail_run(repo, run_id, exc)
        db.close()
        raise
    db.close()
    return result


def replay_latest_odds(args, *, preseason=False) -> dict[str, object]:
    """Normalize the latest preserved Odds API response without a network call."""
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    endpoint = "current_preseason_odds" if preseason else "current_odds"
    sport_key = PRESEASON_SPORT_KEY if preseason else SPORT_KEY
    run_id = repo.begin_run("the_odds_api", f"replay_{endpoint}", now())
    try:
        raw = repo.latest_raw_ingest("the_odds_api", endpoint)
        if raw is None:
            raise ValueError(f"No preserved {endpoint.replace('_', ' ')} response is available to replay")
        payload = json.loads(raw["payload_json"])
        if not isinstance(payload, list):
            raise ValueError("The preserved current odds response is not an event list")
        collected_at = datetime.fromisoformat(raw["collected_at"])
        parsed = parse_current_spreads(
            payload,
            MappingTeamResolver(alias_mapping(db)),
            collected_at,
            sport_key=sport_key,
        )
        batch = CollectionBatch(
            provider_key=parsed.provider_key,
            collected_at=parsed.collected_at,
            raw_payload=parsed.raw_payload,
            quotes=parsed.quotes,
            warnings=parsed.warnings,
            metadata={"endpoint": endpoint, "request_cost": 0},
        )
        stored, warnings = repo.persist_odds(
            batch,
            accessible_bookmakers=settings.accessible_bookmakers,
            consensus_bookmakers=settings.market_consensus_bookmakers,
            stale_after_minutes=settings.odds_stale_after_minutes,
        )
        _complete_run(repo, run_id, batch, stored, warnings)
        return {
            "received": len(batch.quotes),
            "stored": stored,
            "warnings": list(warnings),
            "reused_ingest_id": raw["ingest_id"],
            "new_api_requests": 0,
        }
    except Exception as exc:
        _fail_run(repo, run_id, exc)
        raise
    finally:
        db.close()


def diagnose_preseason_odds(args) -> dict[str, object]:
    """Explain unmatched preserved preseason events without an API request."""
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    try:
        raw = repo.latest_raw_ingest("the_odds_api", "current_preseason_odds")
        if raw is None:
            raise ValueError("No preserved current preseason odds response is available")
        payload = json.loads(raw["payload_json"])
        if not isinstance(payload, list):
            raise ValueError("The preserved preseason odds response is not an event list")

        resolver = MappingTeamResolver(alias_mapping(db))
        unmatched: list[dict[str, object]] = []
        for event in payload:
            event_id = str(event.get("id", "<missing>"))
            mapped = db.execute(
                """
                SELECT game_id FROM game_source_ids
                WHERE source_key = 'the_odds_api' AND source_game_id = ?
                """,
                (event_id,),
            ).fetchone()
            if mapped:
                continue

            home_name = str(event.get("home_team", ""))
            away_name = str(event.get("away_team", ""))
            provider_kickoff = parse_timestamp(str(event.get("commence_time", "")))
            home_id = resolver.resolve(home_name)
            away_id = resolver.resolve(away_name)
            candidates = db.execute(
                """
                SELECT g.game_id, g.home_team_id, g.away_team_id, g.kickoff_time,
                       ht.official_name AS schedule_home,
                       at.official_name AS schedule_away
                FROM games g
                JOIN teams ht ON ht.team_id = g.home_team_id
                JOIN teams at ON at.team_id = g.away_team_id
                WHERE g.season_type = 'preseason'
                  AND ((g.home_team_id = ? AND g.away_team_id = ?)
                    OR (g.home_team_id = ? AND g.away_team_id = ?))
                """,
                (home_id, away_id, away_id, home_id),
            ).fetchall()
            ranked = sorted(
                candidates,
                key=lambda row: abs(
                    datetime.fromisoformat(row["kickoff_time"]) - provider_kickoff
                ),
            )
            nearest = ranked[0] if ranked else None
            if nearest is None:
                reason = "team_pair_not_in_preseason_schedule"
                closest = None
            else:
                difference = abs(
                    datetime.fromisoformat(nearest["kickoff_time"]) - provider_kickoff
                ).total_seconds() / 60
                reversed_sides = (
                    nearest["home_team_id"] == away_id
                    and nearest["away_team_id"] == home_id
                )
                reason = (
                    "home_away_mismatch"
                    if reversed_sides
                    else "kickoff_time_difference_over_10_minutes"
                )
                closest = {
                    "game_id": nearest["game_id"],
                    "matchup": f'{nearest["schedule_away"]} at {nearest["schedule_home"]}',
                    "kickoff": nearest["kickoff_time"],
                    "difference_minutes": round(difference, 1),
                }
            unmatched.append(
                {
                    "event_id": event_id,
                    "provider_matchup": f"{away_name} at {home_name}",
                    "provider_kickoff": provider_kickoff.isoformat(),
                    "reason": reason,
                    "closest_schedule_entry": closest,
                }
            )

        return {
            "unmatched_events": unmatched,
            "unmatched_count": len(unmatched),
            "new_api_requests": 0,
        }
    finally:
        db.close()


def collect_rundown_odds(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    run_id = repo.begin_run(
        "therundown", f"current_odds_{args.sport_id}_{args.date.isoformat()}", now()
    )
    try:
        collector = TheRundownCollector(
            api_key=settings.require_therundown_api_key(),
            resolver=MappingTeamResolver(alias_mapping(db)),
            transport=_transport(settings),
            event_date=args.date,
            sport_id=args.sport_id,
        )
        batch = collector.collect_current_spreads()
        stored, warnings = repo.persist_odds(
            batch,
            accessible_bookmakers=settings.accessible_bookmakers,
            consensus_bookmakers=settings.market_consensus_bookmakers,
            stale_after_minutes=settings.odds_stale_after_minutes,
        )
        _complete_run(repo, run_id, batch, stored, warnings)
        result = {
            "received": len(batch.quotes),
            "stored": stored,
            "warnings": list(warnings),
            "quota": {
                key: batch.metadata.get(key)
                for key in (
                    "datapoints_remaining",
                    "datapoints_used",
                    "datapoints_limit",
                    "datapoints_cost",
                )
            },
        }
    except Exception as exc:
        _fail_run(repo, run_id, exc)
        db.close()
        raise
    db.close()
    return result


def collect_weather(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    run_id = repo.begin_run("nws", f"forecast_{args.game_id}", now())
    game = db.execute(
        """
        SELECT g.game_id, g.kickoff_time, g.roof_status, s.latitude, s.longitude,
               s.weather_exposed, s.country_code
        FROM games g JOIN stadiums s ON s.stadium_id = g.stadium_id
        WHERE g.game_id = ?
        """,
        (args.game_id,),
    ).fetchone()
    try:
        if not game:
            raise ValueError("Unknown game ID")
        if not game["weather_exposed"] or game["roof_status"] in {"closed", "fixed"}:
            repo.finish_run(run_id, finished_at=now(), status="skipped", warnings=("Outdoor weather is not required for this game",))
            return {"status": "skipped", "reason": "Outdoor weather is not required"}
        if game["country_code"] != "US":
            repo.finish_run(run_id, finished_at=now(), status="skipped", warnings=("NWS does not cover this international venue",))
            return {"status": "skipped", "reason": "Another approved weather source is required"}
        collector = NwsCollector(
            transport=_transport(settings),
            user_agent=settings.require_nws_user_agent(),
        )
        batch = collector.collect_forecast(
            latitude=game["latitude"],
            longitude=game["longitude"],
            kickoff_time=datetime.fromisoformat(game["kickoff_time"]),
        )
        stored = repo.persist_weather(
            args.game_id, batch, game["roof_status"] or "unknown"
        )
        repo.finish_run(run_id, finished_at=now(), status="succeeded", received=1, stored=stored)
        return {"received": 1, "stored": stored}
    except Exception as exc:
        _fail_run(repo, run_id, exc)
        raise
    finally:
        db.close()


def check_official(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    repo = CollectorRepository(db)
    collector = OfficialNflCollector(transport=_transport(settings))
    game_ids = [
        row[0]
        for row in db.execute(
            "SELECT game_id FROM games WHERE season = ? AND week = ?",
            (args.season, args.week),
        )
    ]
    results = {}
    for name in ("injuries", "transactions"):
        run_id = repo.begin_run(f"nfl_{name}", f"official_{name}_check", now())
        try:
            check = collector.check(name)
            stored = repo.persist_official_check(check, game_ids=game_ids)
            repo.finish_run(
                run_id,
                finished_at=now(),
                status="succeeded" if check.access_status == "confirmed" else "partial",
                received=1,
                stored=stored,
                warnings=(check.warning,) if check.warning else (),
            )
            results[name] = {"access_status": check.access_status, "games_checked": stored}
        except Exception as exc:
            _fail_run(repo, run_id, exc)
            results[name] = {"status": "failed", "error": str(exc)}
    db.close()
    return results


def status(args) -> dict[str, object]:
    settings = _settings(args)
    db = _open(settings)
    rows = db.execute(
        """
        SELECT source_key, endpoint_name, status, started_at, finished_at,
               records_received, records_stored, error_message
        FROM collection_runs ORDER BY started_at DESC LIMIT 20
        """
    ).fetchall()
    result = {
        "mode": settings.bot_mode,
        "settings": settings.safe_summary(),
        "counts": {
            "games": db.execute("SELECT COUNT(*) FROM games").fetchone()[0],
            "odds_snapshots": db.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0],
            "injury_snapshots": db.execute("SELECT COUNT(*) FROM injury_snapshots").fetchone()[0],
            "roster_snapshots": db.execute("SELECT COUNT(*) FROM roster_snapshots").fetchone()[0],
            "weather_snapshots": db.execute("SELECT COUNT(*) FROM weather_snapshots").fetchone()[0],
        },
        "recent_runs": [dict(row) for row in rows],
    }
    db.close()
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--env", default=".env", help="Protected settings file")
    sub = result.add_subparsers(dest="command", required=True)

    nflverse = sub.add_parser("nflverse", help="Collect schedules, rosters, and injuries")
    nflverse.add_argument("--season", type=int, required=True)
    nflverse.add_argument("--include-pbp", action="store_true")

    preseason = sub.add_parser(
        "preseason", help="Import the reviewed complete official preseason schedule"
    )
    preseason.add_argument("--season", type=int, default=2026)

    sub.add_parser("odds", help="Collect current NFL full-game spreads")
    sub.add_parser(
        "preseason-odds", help="Collect current NFL preseason full-game spreads"
    )
    sub.add_parser(
        "replay-odds", help="Reprocess the latest stored Odds API response without a request"
    )
    sub.add_parser(
        "replay-preseason-odds",
        help="Reprocess the latest stored preseason Odds API response without a request",
    )
    sub.add_parser(
        "diagnose-preseason-odds",
        help="Explain unmatched preserved preseason odds without a request",
    )

    rundown = sub.add_parser(
        "rundown-odds", help="Collect TheRundown NFL full-game spreads for one date"
    )
    rundown.add_argument("--date", type=date.fromisoformat, required=True)
    rundown.add_argument("--sport-id", type=int, choices=(2, 25, 26), required=True)

    historical = sub.add_parser("historical-odds", help="Collect one licensed historical snapshot")
    historical.add_argument("--at", required=True, help="ISO-8601 timestamp")

    weather = sub.add_parser("weather", help="Collect NWS weather for one stored game")
    weather.add_argument("--game-id", required=True)

    official = sub.add_parser("official-checks", help="Check official NFL injury and transaction pages")
    official.add_argument("--season", type=int, required=True)
    official.add_argument("--week", type=int, required=True)

    sub.add_parser("status", help="Show safe collector status")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "nflverse":
            output = collect_nflverse(args)
        elif args.command == "preseason":
            output = collect_preseason(args)
        elif args.command == "odds":
            output = collect_odds(args)
        elif args.command == "preseason-odds":
            output = collect_odds(args, preseason=True)
        elif args.command == "replay-odds":
            output = replay_latest_odds(args)
        elif args.command == "replay-preseason-odds":
            output = replay_latest_odds(args, preseason=True)
        elif args.command == "diagnose-preseason-odds":
            output = diagnose_preseason_odds(args)
        elif args.command == "rundown-odds":
            output = collect_rundown_odds(args)
        elif args.command == "historical-odds":
            output = collect_odds(args, historical=True)
        elif args.command == "weather":
            output = collect_weather(args)
        elif args.command == "official-checks":
            output = check_official(args)
        else:
            output = status(args)
        print(json.dumps(output, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
