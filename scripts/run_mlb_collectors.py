#!/usr/bin/env python3
"""Run our MLB Phase 2 read-only collectors for one explicit date.

This command stores schedule/probable-pitcher information, current h2h moneylines,
and reachability checks for required official MLB pages.  It never issues a pick,
takes a real-money action, or changes BOT_MODE from test_only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.collectors.base import MappingTeamResolver
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.mlb_official import (
    MappingStadiumResolver,
    MlbRecentResultsCollector,
    MlbScheduleCollector,
    OfficialMlbPageCollector,
)
from sports_analytics_pipeline.collectors.storage import CollectorRepository
from sports_analytics_pipeline.collectors.the_odds_api import MLB_SPORT_KEY, TheOddsApiCollector
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import (
    alias_mapping,
    seed_reference_data,
    stadium_alias_mapping,
    stadium_provider_id_mapping,
)
from sports_analytics_pipeline.mlb_live_sources import (
    AffiliateAwareMlbTransactionsCollector,
    add_bookmaker_response_diagnostics,
)
from sports_analytics_pipeline.mlb_research import build_and_store_mlb_research_contexts
from sports_analytics_pipeline.mlb_weather import collect_required_mlb_weather
from sports_analytics_pipeline.quota_budget import DailyCreditBudget, poll_due, record_poll


ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"


def _quota_header(headers, name: str) -> int | None:
    try:
        value = headers.get(name.casefold())
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect MLB Phase 2 pregame data in test_only mode")
    parser.add_argument("date", help="Explicit MLB schedule date in YYYY-MM-DD form")
    parser.add_argument("--env", default=".env")
    parser.add_argument("--skip-pages", action="store_true", help="Skip official MLB page reachability checks")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("date must use YYYY-MM-DD") from exc

    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("MLB Phase 2 requires a supported research mode")

    db = connect(settings.database_path)
    initialize_schema(db)
    seed_reference_data(db, include_mlb=True)
    repo = CollectorRepository(db)
    transport = UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )
    teams = MappingTeamResolver(alias_mapping(db, "mlb"))
    stadiums = MappingStadiumResolver(
        stadium_alias_mapping(db, "mlb"),
        provider_ids=stadium_provider_id_mapping("mlb"),
    )
    output: dict[str, object] = {"date": args.date, "mode": settings.bot_mode}

    schedule_run = repo.begin_run("mlb_schedule", f"schedule_{args.date}", utc_now())
    try:
        schedule = MlbScheduleCollector(
            transport=transport, team_resolver=teams, stadium_resolver=stadiums
        ).collect_date(args.date)
        schedule_stored = repo.persist_mlb_schedule(schedule)
        repo.finish_run(
            schedule_run,
            finished_at=utc_now(),
            status="partial" if schedule.warnings else "succeeded",
            received=len(schedule.games),
            stored=schedule_stored,
            warnings=schedule.warnings,
        )
        output["schedule"] = {
            "received": len(schedule.games),
            "stored": schedule_stored,
            "warnings": list(schedule.warnings),
        }
    except Exception as exc:
        repo.finish_run(schedule_run, finished_at=utc_now(), status="failed", error_message=str(exc))
        output["schedule"] = {"status": "failed", "error": str(exc)}

    odds_run = repo.begin_run("the_odds_api", "current_mlb_odds", utc_now())
    try:
        quota_response = transport.get(
            f"{ODDS_API_BASE_URL}/sports/",
            params={"apiKey": settings.require_odds_api_key()},
        )
        quota_metadata = {
            "requests_remaining": _quota_header(
                quota_response.headers, "x-requests-remaining"
            ),
            "requests_used": _quota_header(quota_response.headers, "x-requests-used"),
        }
        odds_budget = DailyCreditBudget.from_metadata(
            db,
            provider_key="the_odds_api_mlb",
            metadata=quota_metadata,
            as_of=quota_response.received_at,
            reserve_credits=settings.odds_api_min_remaining_credits,
            daily_credit_cap=settings.odds_api_daily_credit_cap,
        )
        refresh_due = poll_due(
            db,
            provider_key="the_odds_api_mlb",
            sport="mlb",
            endpoint_name="odds",
            scope_key=MLB_SPORT_KEY,
            as_of=quota_response.received_at,
            interval_minutes=settings.multisport_odds_refresh_minutes,
        )
        if not refresh_due:
            warning = (
                "MLB odds snapshot deferred until the paid "
                f"{settings.multisport_odds_refresh_minutes}-minute refresh interval."
            )
            repo.finish_run(
                odds_run,
                finished_at=utc_now(),
                status="succeeded",
                received=0,
                stored=0,
                warnings=(warning,),
                metadata={"quota_budget": odds_budget.as_dict()},
            )
            output["odds"] = {
                "status": "refresh_deferred",
                "received": 0,
                "stored": 0,
                "warnings": [warning],
                "quota_budget": odds_budget.as_dict(),
            }
        elif not odds_budget.reserve(1):
            warning = (
                "MLB odds snapshot deferred so The Odds API credits remain available "
                "for the full month."
            )
            repo.finish_run(
                odds_run,
                finished_at=utc_now(),
                status="succeeded",
                received=0,
                stored=0,
                warnings=(warning,),
                metadata={"quota_budget": odds_budget.as_dict()},
            )
            output["odds"] = {
                "status": "budget_deferred",
                "received": 0,
                "stored": 0,
                "warnings": [warning],
                "quota_budget": odds_budget.as_dict(),
            }
        else:
            configured_books = tuple(dict.fromkeys(
                settings.accessible_bookmakers + settings.market_consensus_bookmakers
            ))
            odds = TheOddsApiCollector(
                api_key=settings.require_odds_api_key(),
                resolver=teams,
                transport=transport,
                bookmakers=configured_books,
                regions=() if configured_books else ("us",),
                sport_key=MLB_SPORT_KEY,
            ).collect_current_spreads()
            odds_budget.reconcile(odds.metadata)
            odds = add_bookmaker_response_diagnostics(
                odds,
                requested_bookmakers=configured_books,
            )
            record_poll(
                db,
                provider_key="the_odds_api_mlb",
                sport="mlb",
                endpoint_name="odds",
                scope_key=MLB_SPORT_KEY,
                polled_at=odds.collected_at,
                request_cost=odds.metadata.get("request_cost"),
                requests_remaining=odds.metadata.get("requests_remaining"),
            )
            missing_requested = tuple(odds.metadata.get("missing_requested_bookmakers") or ())
            missing_accessible = [
                book for book in settings.accessible_bookmakers if book in missing_requested
            ]
            stored, warnings = repo.persist_odds(
                odds,
                accessible_bookmakers=settings.accessible_bookmakers,
                consensus_bookmakers=settings.market_consensus_bookmakers,
                stale_after_minutes=settings.odds_stale_after_minutes,
            )
            all_warnings = tuple(odds.warnings) + tuple(warnings)
            repo.finish_run(
                odds_run,
                finished_at=utc_now(),
                status="partial" if all_warnings else "succeeded",
                received=len(odds.quotes),
                stored=stored,
                warnings=all_warnings,
                metadata={**odds.metadata, "quota_budget": odds_budget.as_dict()},
            )
            output["odds"] = {
                "received": len(odds.quotes),
                "stored": stored,
                "warnings": list(all_warnings),
                "requested_bookmakers": list(odds.metadata.get("requested_bookmakers") or ()),
                "returned_bookmakers": list(odds.metadata.get("returned_bookmakers") or ()),
                "missing_requested_bookmakers": list(missing_requested),
                "missing_accessible_bookmakers": missing_accessible,
                "quota": {
                    key: odds.metadata.get(key)
                    for key in ("requests_remaining", "requests_used", "request_cost")
                },
                "quota_budget": odds_budget.as_dict(),
            }
    except Exception as exc:
        repo.finish_run(odds_run, finished_at=utc_now(), status="failed", error_message=str(exc))
        output["odds"] = {"status": "failed", "error": str(exc)}


    recent_run = repo.begin_run("mlb_schedule", f"recent_results_{args.date}", utc_now())
    try:
        recent = MlbRecentResultsCollector(
            transport=transport, team_resolver=teams
        ).collect_before(args.date, lookback_days=21)
        recent_stored = repo.persist_mlb_recent_results(recent)
        repo.finish_run(
            recent_run,
            finished_at=utc_now(),
            status="partial" if recent.warnings else "succeeded",
            received=len(recent.games),
            stored=recent_stored,
            warnings=recent.warnings,
            metadata={
                "window_start": recent.start_date,
                "window_end": recent.end_date,
            },
        )
        output["recent_results"] = {
            "window_start": recent.start_date,
            "window_end": recent.end_date,
            "received": len(recent.games),
            "stored": recent_stored,
            "warnings": list(recent.warnings),
        }
    except Exception as exc:
        repo.finish_run(
            recent_run,
            finished_at=utc_now(),
            status="failed",
            error_message=str(exc),
        )
        output["recent_results"] = {"status": "failed", "error": str(exc)}

    transaction_run = repo.begin_run("mlb_transactions", f"transactions_{args.date}", utc_now())
    try:
        transactions = AffiliateAwareMlbTransactionsCollector(
            transport=transport, team_resolver=teams
        ).collect_window(args.date, lookback_days=7)
        transaction_counts = repo.persist_mlb_transactions(transactions)
        repo.finish_run(
            transaction_run,
            finished_at=utc_now(),
            status="partial" if transactions.warnings else "succeeded",
            received=len(transactions.records),
            stored=transaction_counts["transactions"],
            warnings=transactions.warnings,
            metadata={
                "window_start": transactions.start_date,
                "window_end": transactions.end_date,
                "roster_snapshots": transaction_counts["rosters"],
                "injury_snapshots": transaction_counts["injuries"],
            },
        )
        output["structured_transactions"] = {
            "window_start": transactions.start_date,
            "window_end": transactions.end_date,
            "received": len(transactions.records),
            "transactions_stored": transaction_counts["transactions"],
            "roster_snapshots_stored": transaction_counts["rosters"],
            "injury_snapshots_stored": transaction_counts["injuries"],
            "warnings": list(transactions.warnings),
        }
    except Exception as exc:
        repo.finish_run(
            transaction_run,
            finished_at=utc_now(),
            status="failed",
            error_message=str(exc),
        )
        output["structured_transactions"] = {"status": "failed", "error": str(exc)}

    if not args.skip_pages:
        checks = []
        checker = OfficialMlbPageCollector(transport=transport)
        game_ids = (
            tuple(f"mlb_{game.source_game_id}" for game in schedule.games)
            if "schedule" in locals()
            else ()
        )
        for check_type in ("injuries", "lineups", "transactions", "probable_pitchers"):
            try:
                check = checker.check(check_type)
                stored_checks = repo.persist_mlb_official_check(check, game_ids=game_ids)
                checks.append({
                    "type": check.check_type,
                    "source": check.source_key,
                    "status": check.access_status,
                    "checked_at": check.checked_at.isoformat(),
                    "warning": check.warning,
                    "game_checks_stored": stored_checks,
                })
            except Exception as exc:
                checks.append({"type": check_type, "status": "failed", "error": str(exc)})
        output["official_page_checks"] = checks

    game_ids = (
        tuple(f"mlb_{game.source_game_id}" for game in schedule.games)
        if "schedule" in locals()
        else ()
    )
    if game_ids:
        output["weather"] = collect_required_mlb_weather(
            db,
            repo=repo,
            transport=transport,
            user_agent=settings.nws_user_agent,
            game_ids=game_ids,
            now_fn=utc_now,
        )

        research_as_of = utc_now()
        research_contexts = []
        for game_id in game_ids:
            try:
                research_contexts.extend(
                    build_and_store_mlb_research_contexts(
                        db,
                        game_ids=(game_id,),
                        as_of=research_as_of,
                        stale_after_minutes=settings.odds_stale_after_minutes,
                    )
                )
            except Exception as exc:
                research_contexts.append({
                    "game_id": game_id,
                    "research_complete": False,
                    "status": "failed_closed",
                    "error": str(exc),
                })
        output["reviewed_research_contexts"] = research_contexts

    output["notice"] = "Research/paper-tracking collection only. No real-money action or official pick is produced."
    print(json.dumps(output, indent=2, sort_keys=True))
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

