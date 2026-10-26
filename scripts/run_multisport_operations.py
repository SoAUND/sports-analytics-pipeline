#!/usr/bin/env python3
"""Command-line entry point for our multi-sport test-only operations."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.collectors.http import UrllibTransport
from sports_analytics_pipeline.collectors.provider_services import (
    CLIENTS,
    configuration_report,
    live_probe_report,
    routing_report,
    safe_provider_report,
)
from sports_analytics_pipeline.collectors.multisport_history_api import (
    collect_historical_data,
    historical_collection_status,
)
from sports_analytics_pipeline.collectors.multisport_live_research import (
    collect_automatic_live_research,
)
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.epl_history_backfill import backfill_epl_result_features
from sports_analytics_pipeline.multisport_features import training_feature_version
from sports_analytics_pipeline.multisport_history import (
    history_status,
    import_history_rows,
    load_csv_history,
    load_json_history,
)
from sports_analytics_pipeline.multisport_history_builder import (
    SUPPORTED_BUILD_SPORTS,
    build_history_examples,
    import_feature_evidence,
    load_feature_evidence,
)
from sports_analytics_pipeline.multisport_modeling import latest_model, train_model
from sports_analytics_pipeline.multisport_operations import ALL_SPORTS, bootstrap, run_cycle, status_report
from sports_analytics_pipeline.multisport_preflight import MIN_TRAINING_EXAMPLES, preflight_report
from sports_analytics_pipeline.multisport_research_pipeline import StructuredResearchAdapter, run_research_adapters
from sports_analytics_pipeline.multisport_results import record_manual_validation_review
from sports_analytics_pipeline.multisport_runtime import set_manual_model_approval


UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _sports(value: str) -> tuple[str, ...]:
    result = tuple(item.strip().casefold() for item in value.split(",") if item.strip())
    invalid = [item for item in result if item not in ALL_SPORTS]
    if invalid:
        raise argparse.ArgumentTypeError(f"Unsupported sports: {', '.join(invalid)}")
    return result


def _providers(value: str) -> tuple[str, ...]:
    result = tuple(item.strip().casefold() for item in value.split(",") if item.strip())
    invalid = [item for item in result if item not in CLIENTS]
    if invalid:
        raise argparse.ArgumentTypeError(f"Unsupported providers: {', '.join(invalid)}")
    return result


def _history_builder_sports(value: str) -> tuple[str, ...]:
    result = _sports(value)
    invalid = [item for item in result if item not in SUPPORTED_BUILD_SPORTS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"Historical example building is not implemented for: {', '.join(invalid)}"
        )
    return result


def _bookmakers(value: str) -> tuple[str, ...]:
    result = tuple(item.strip() for item in value.split(",") if item.strip())
    if not result:
        raise argparse.ArgumentTypeError("At least one bookmaker name is required")
    if len(result) > 30:
        raise argparse.ArgumentTypeError("At most 30 bookmaker names are allowed")
    return result


def _connection(settings: Settings):
    db = connect(settings.database_path)
    initialize_schema(db)
    return db


def command_bootstrap(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        print(json.dumps(bootstrap(db, as_of=_now()), indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


def command_cycle(args) -> int:
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"test_only", "research"}:
        raise SystemExit("Unsupported research mode")
    settings.require_odds_api_key()
    if not settings.sportsbook_lists_approved():
        raise SystemExit("MARKET_CONSENSUS_BOOKMAKERS and ACCESSIBLE_BOOKMAKERS must be configured")
    db = _connection(settings)
    try:
        result = run_cycle(db, settings=settings, sports=args.sports, as_of=_now())
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_status(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        print(json.dumps(
            status_report(
                db,
                paper_unit_dollars=args.paper_unit_dollars,
                selection_horizon_minutes=settings.multisport_odds_horizon_minutes,
                odds_stale_after_minutes=settings.odds_stale_after_minutes,
            ),
            indent=2,
            sort_keys=True,
            default=str,
        ))
    finally:
        db.close()
    return 0


def command_preflight(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        report = preflight_report(db, settings=settings)
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_provider_health(args) -> int:
    """Report secret presence by default and make live API calls only when requested."""
    settings = Settings.load(args.env)
    provider_keys = args.providers or tuple(CLIENTS)
    if args.require_all:
        try:
            settings.require_provider_api_keys(provider_keys)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    if args.live:
        transport = UrllibTransport(
            timeout_seconds=settings.http_timeout_seconds,
            attempts=settings.http_retry_attempts,
        )
        probes = live_probe_report(
            settings,
            transport=transport,
            provider_keys=provider_keys,
        )
    else:
        configured = configuration_report(settings)
        probes = {key: configured[key] for key in provider_keys}
    output = {
        "mode": "live" if args.live else "configuration_only",
        "providers": safe_provider_report(probes),
        "routing": routing_report(settings),
        "automatic_odds_fallback": False,
    }
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    if args.live and any(probe.reachable is not True for probe in probes.values()):
        return 1
    return 0


def command_history_status(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        print(json.dumps(history_status(db), indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


def command_history_collection_status(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        print(json.dumps(historical_collection_status(db), indent=2, sort_keys=True))
    finally:
        db.close()
    return 0


def command_history_import(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        path = Path(args.path)
        rows = load_json_history(path) if path.suffix.casefold() == ".json" else load_csv_history(path)
        result = import_history_rows(db, rows, created_at=_now())
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["rejected"] else 0
    finally:
        db.close()


def command_history_evidence_import(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        rows = load_feature_evidence(args.path)
        result = import_feature_evidence(db, rows, collected_at=_now())
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["rejected"] else 0
    finally:
        db.close()


def command_history_build_examples(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        result = build_history_examples(
            db,
            sports=args.sports,
            built_at=_now(),
            minimum_examples=args.min_examples,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_history_backfill_epl_features(args) -> int:
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"test_only", "research"}:
        raise SystemExit("Unsupported research mode")
    if not settings.football_data_api_key:
        raise SystemExit("FOOTBALL_DATA_API_KEY is required for EPL feature backfill")
    transport = UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )
    db = _connection(settings)
    try:
        result = backfill_epl_result_features(
            db,
            api_key=settings.football_data_api_key,
            transport=transport,
            collected_at=_now(),
            max_requests=args.max_requests,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["status"] == "failed" else 0
    finally:
        db.close()


def _history_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Historical timestamps must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("Historical timestamps must include a time zone")
    return parsed.astimezone(UTC)


def command_history_collect(args) -> int:
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"test_only", "research"}:
        raise SystemExit("Unsupported research mode")
    if not settings.odds_api_io_key:
        raise SystemExit("ODDS_API_IO_KEY is required for automatic historical collection")

    end = args.end or _now().replace(hour=0, minute=0, second=0, microsecond=0)
    start = args.start or (end - timedelta(days=settings.multisport_history_lookback_days))
    if start >= end:
        raise SystemExit("Historical --start must be earlier than --end")
    bookmakers = args.bookmakers or settings.odds_api_io_history_bookmakers
    if args.include_odds and not bookmakers:
        raise SystemExit(
            "ODDS_API_IO_HISTORY_BOOKMAKERS or --bookmakers is required with --include-odds"
        )

    transport = UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )
    db = _connection(settings)
    try:
        result = collect_historical_data(
            db,
            api_key=settings.odds_api_io_key,
            transport=transport,
            sports=args.sports,
            start=start,
            end=end,
            bookmakers=bookmakers,
            include_odds=args.include_odds,
            max_requests=args.max_requests or settings.multisport_history_max_requests,
            max_leagues_per_sport=(
                args.max_leagues or settings.multisport_history_max_leagues_per_sport
            ),
            max_odds_events=(
                args.max_odds_events
                if args.max_odds_events is not None
                else settings.multisport_history_max_odds_events
            ),
            collected_at=_now(),
            verified_epl_only=args.verified_epl_odds_only,
            odds_only=args.odds_only,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["status"] == "failed" else 0
    finally:
        db.close()


def command_research_import(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SystemExit("Research import JSON must be a list of records")
        source_keys = sorted({str(row.get("source_key", "")) for row in payload if row.get("source_key")})
        results = []
        for source_key in source_keys:
            adapter = StructuredResearchAdapter(source_key, payload)
            results.append(
                run_research_adapters(
                    db,
                    event_id=args.event_id,
                    as_of=_now(),
                    adapters=(adapter,),
                    max_age_minutes=args.max_age_minutes,
                )
            )
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_research_collect(args) -> int:
    """Collect every configured normalized live feed once, within Our request cap."""
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"test_only", "research"}:
        raise SystemExit("Unsupported research mode")
    transport = UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        result = collect_automatic_live_research(
            db,
            sports=args.sports,
            as_of=_now(),
            feed_urls=settings.multisport_research_feed_urls,
            bearer_token=settings.multisport_research_feed_token,
            transport=transport,
            horizon_minutes=settings.multisport_research_horizon_minutes,
            max_requests=settings.multisport_research_max_requests,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["status"] == "completed_with_blockers" else 0
    finally:
        db.close()


def _parse_cutoff(value: str | None) -> datetime:
    if value is None:
        return _now()
    cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise SystemExit("--training-cutoff must include a time zone")
    return cutoff.astimezone(UTC)


def command_train(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        cutoff = _parse_cutoff(args.training_cutoff)
        model = train_model(
            db,
            sport=args.sport,
            feature_version=training_feature_version(args.sport),
            training_cutoff=cutoff,
            min_examples=args.min_examples,
            holdout_fraction=args.holdout_fraction,
            trained_at=_now(),
        )
        print(json.dumps(model.__dict__, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_train_all(args) -> int:
    """Train every sport that has enough approved historical rows; never fabricate rows."""
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        bootstrap(db, as_of=_now())
        histories = history_status(db)
        cutoff = _parse_cutoff(args.training_cutoff)
        results: dict[str, object] = {}
        for sport in args.sports:
            current = latest_model(db, sport)
            if current is not None and not args.retrain:
                results[sport] = {
                    "status": "skipped",
                    "reason": "A test-only model already exists; use --retrain for a new immutable version.",
                    "model_version": str(current["model_version"]),
                }
                continue
            if histories[sport] < args.min_examples:
                results[sport] = {
                    "status": "blocked",
                    "reason": f"Historical examples {histories[sport]}/{args.min_examples}",
                }
                continue
            try:
                model = train_model(
                    db,
                    sport=sport,
                    feature_version=training_feature_version(sport),
                    training_cutoff=cutoff,
                    min_examples=args.min_examples,
                    holdout_fraction=args.holdout_fraction,
                    trained_at=_now(),
                )
                results[sport] = {"status": "trained", **model.__dict__}
            except Exception as exc:
                results[sport] = {"status": "blocked", "reason": str(exc)}
        print(json.dumps(results, indent=2, sort_keys=True, default=str))
    finally:
        db.close()
    return 0


def command_validation_review(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        record_manual_validation_review(
            db,
            sport=args.sport,
            calibration_status=args.calibration,
            clv_status=args.clv,
            passed=args.passed,
            reviewed_at=_now(),
        )
        print(f"Recorded manual validation review for {args.sport}.")
    finally:
        db.close()
    return 0


def command_model_approval(args) -> int:
    settings = Settings.load(args.env)
    db = _connection(settings)
    try:
        set_manual_model_approval(
            db,
            sport=args.sport,
            model_version=args.model_version,
            status=args.status,
            approved_by=args.approved_by,
            notes=args.notes,
            approved_at=_now(),
        )
        print(f"Model status for {args.sport}: {args.status}")
    finally:
        db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Our multi-sport paper-testing operations")
    parser.add_argument("--env", default=".env", help="Environment file path")
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = sub.add_parser("bootstrap")
    bootstrap_parser.set_defaults(func=command_bootstrap)

    cycle_parser = sub.add_parser("cycle")
    cycle_parser.add_argument("--sports", type=_sports, default=ALL_SPORTS)
    cycle_parser.set_defaults(func=command_cycle)

    status_parser = sub.add_parser("status")
    status_parser.add_argument(
        "--paper-unit-dollars",
        type=float,
        default=10.0,
        help="Dollar example used to explain one paper unit (default: 10)",
    )
    status_parser.set_defaults(func=command_status)

    preflight_parser = sub.add_parser("preflight")
    preflight_parser.set_defaults(func=command_preflight)

    provider_parser = sub.add_parser(
        "provider-health",
        help="Verify API secret presence; add --live for one read-only probe per provider",
    )
    provider_parser.add_argument("--providers", type=_providers)
    provider_parser.add_argument("--live", action="store_true")
    provider_parser.add_argument("--require-all", action="store_true")
    provider_parser.set_defaults(func=command_provider_health)

    history_status_parser = sub.add_parser("history-status")
    history_status_parser.set_defaults(func=command_history_status)

    history_collection_status_parser = sub.add_parser("history-collection-status")
    history_collection_status_parser.set_defaults(func=command_history_collection_status)

    history_parser = sub.add_parser("history-import")
    history_parser.add_argument("path")
    history_parser.set_defaults(func=command_history_import)

    history_evidence_parser = sub.add_parser(
        "history-evidence-import",
        help="Import timestamped pre-event feature evidence without automatic promotion",
    )
    history_evidence_parser.add_argument("path")
    history_evidence_parser.set_defaults(func=command_history_evidence_import)

    history_build_parser = sub.add_parser(
        "history-build-examples",
        help="Build only complete leakage-safe historical training examples",
    )
    history_build_parser.add_argument(
        "--sports",
        type=_history_builder_sports,
        default=SUPPORTED_BUILD_SPORTS,
    )
    history_build_parser.add_argument("--min-examples", type=int, default=MIN_TRAINING_EXAMPLES)
    history_build_parser.set_defaults(func=command_history_build_examples)

    history_backfill_epl_parser = sub.add_parser(
        "history-backfill-epl-features",
        help="Backfill safe result-derived EPL features from football-data.org",
    )
    history_backfill_epl_parser.add_argument("--max-requests", type=int, default=4)
    history_backfill_epl_parser.set_defaults(func=command_history_backfill_epl_features)

    history_collect_parser = sub.add_parser(
        "history-collect",
        help="Collect bounded settled events and optional closing odds from Odds-API.io",
    )
    history_collect_parser.add_argument("--sports", type=_sports, default=ALL_SPORTS)
    history_collect_parser.add_argument("--start", type=_history_time)
    history_collect_parser.add_argument("--end", type=_history_time)
    history_collect_parser.add_argument("--bookmakers", type=_bookmakers)
    history_collect_parser.add_argument("--include-odds", action="store_true")
    history_collect_parser.add_argument(
        "--verified-epl-odds-only",
        action="store_true",
        help="Fetch odds only for EPL events verified by the official results archive",
    )
    history_collect_parser.add_argument(
        "--odds-only",
        action="store_true",
        help="Backfill stored event odds without rediscovering sports, leagues, or events",
    )
    history_collect_parser.add_argument("--max-requests", type=int)
    history_collect_parser.add_argument("--max-leagues", type=int)
    history_collect_parser.add_argument("--max-odds-events", type=int)
    history_collect_parser.set_defaults(func=command_history_collect)

    research_parser = sub.add_parser("research-import")
    research_parser.add_argument("event_id")
    research_parser.add_argument("path")
    research_parser.add_argument("--max-age-minutes", type=int, default=1440)
    research_parser.set_defaults(func=command_research_import)

    research_collect_parser = sub.add_parser(
        "research-collect",
        help="Collect bounded event-specific research from configured HTTPS feeds",
    )
    research_collect_parser.add_argument("--sports", type=_sports, default=ALL_SPORTS)
    research_collect_parser.set_defaults(func=command_research_collect)

    train_parser = sub.add_parser("train")
    train_parser.add_argument("sport", choices=ALL_SPORTS)
    train_parser.add_argument("--training-cutoff", required=True)
    train_parser.add_argument("--min-examples", type=int, default=MIN_TRAINING_EXAMPLES)
    train_parser.add_argument("--holdout-fraction", type=float, default=0.20)
    train_parser.set_defaults(func=command_train)

    train_all_parser = sub.add_parser("train-all")
    train_all_parser.add_argument("--sports", type=_sports, default=ALL_SPORTS)
    train_all_parser.add_argument("--training-cutoff")
    train_all_parser.add_argument("--min-examples", type=int, default=MIN_TRAINING_EXAMPLES)
    train_all_parser.add_argument("--holdout-fraction", type=float, default=0.20)
    train_all_parser.add_argument("--retrain", action="store_true")
    train_all_parser.set_defaults(func=command_train_all)

    review_parser = sub.add_parser("validation-review")
    review_parser.add_argument("sport", choices=ALL_SPORTS)
    review_parser.add_argument("--calibration", choices=("acceptable", "unacceptable"), required=True)
    review_parser.add_argument("--clv", choices=("stable", "favorable", "unfavorable"), required=True)
    review_parser.add_argument("--passed", action="store_true")
    review_parser.set_defaults(func=command_validation_review)

    approval_parser = sub.add_parser("model-approval")
    approval_parser.add_argument("sport", choices=ALL_SPORTS)
    approval_parser.add_argument("model_version")
    approval_parser.add_argument("--status", choices=("test_only", "eligible_for_manual_approval", "approved", "rejected"), required=True)
    approval_parser.add_argument("--approved-by", required=True)
    approval_parser.add_argument("--notes", default="")
    approval_parser.set_defaults(func=command_model_approval)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

