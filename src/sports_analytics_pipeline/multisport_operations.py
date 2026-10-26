"""Operations coordinator for collection, paper evaluation, settlement, and status.

Every automatic cycle remains read-only toward sportsbooks. It may collect data and
record paper decisions, but it cannot take a real-money action or approve a model.

The Odds API usage policy matters here: /sports and /events are quota-free, while
/participants and /odds consume credits. The coordinator therefore discovers events
first and only spends credits when an event is inside our pregame decision horizon.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Iterable

from .collectors.http import HttpTransport, UrllibTransport
from .collectors.multisport_odds import (
    BASE_URL,
    PROVIDER_KEY,
    MultiSportCollectionBatch,
    MultiSportOddsCollector,
    MultiSportParticipant,
    ParticipantDirectory,
    SportsCatalogBatch,
    parse_events,
    parse_timestamp,
)
from .collectors.multisport_scores import AUTO_SCORE_SPORTS, MultiSportScoresCollector
from .collectors.multisport_mma_results import (
    PROVIDER_KEY as MMA_RESULT_PROVIDER,
    ApiSportsMmaResultsCollector,
)
from .collectors.multisport_live_research import collect_automatic_live_research
from .config import Settings
from .multisport_evaluation import decision_presentation, evaluate_event
from .multisport_history import history_status
from .multisport_modeling import latest_model, predict_event
from .multisport_market_baseline import (
    BASELINE_FEATURE_VERSION,
    SUPPORTED_SPORTS as MARKET_BASELINE_SPORTS,
    create_market_baseline_prediction,
    ensure_market_baseline_model,
)
from .epl_live_features import record_epl_baseline_live_features
from .multisport_preflight import MIN_TRAINING_EXAMPLES
from .quota_budget import DailyCreditBudget, poll_due, record_poll
from .multisport_research import (
    live_readiness_report,
    research_gate,
    research_policy_summary,
)
from .multisport_research_pipeline import mark_current_odds_research
from .multisport_results import record_result, settle_ready_decisions
from .multisport_runtime import ensure_runtime_schema, seed_settlement_rules
from .multisport_score_storage import persist_score_batch
from .multisport_storage import (
    seed_validation_state,
    store_events,
    store_odds,
    store_participants,
    store_raw_ingest,
)
from .sports_registry import discover_active_provider_keys, get_sport_market, validation_bucket


UTC = timezone.utc
ALL_SPORTS = ("nba", "wnba", "epl", "mma", "atp", "wta")
SCORE_GRACE_MINUTES = {"nba": 180, "wnba": 180, "epl": 150, "mma": 240, "atp": 180, "wta": 180}
PARTICIPANT_REQUEST_COST = 1
ODDS_REQUEST_COST = 1
SCORE_REQUEST_COST = 2


def bootstrap(connection: sqlite3.Connection, *, as_of: datetime) -> dict[str, int]:
    ensure_runtime_schema(connection)
    validation_rows = seed_validation_state(connection, as_of=as_of)
    seed_settlement_rules(connection, verified_at=as_of)
    for sport in ALL_SPORTS:
        connection.execute(
            """
            INSERT OR IGNORE INTO multisport_validation_metrics (
                validation_bucket, completed_count, wins, losses, pushes, voids,
                calibration_status, clv_status, review_status, updated_at
            ) VALUES (?, 0, 0, 0, 0, 0, 'untested', 'unknown', 'collecting', ?)
            """,
            (validation_bucket(sport), as_of.astimezone(UTC).isoformat()),
        )
    connection.commit()
    return {"validation_buckets": validation_rows, "settlement_rules": len(ALL_SPORTS)}


def _transport(settings: Settings) -> UrllibTransport:
    return UrllibTransport(
        timeout_seconds=settings.http_timeout_seconds,
        attempts=settings.http_retry_attempts,
    )


def _header_int(headers, name: str) -> int | None:
    raw = headers.get(name.casefold())
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _collect_free_event_feed(
    *,
    api_key: str,
    transport: HttpTransport,
    sport: str,
    provider_sport_key: str,
) -> MultiSportCollectionBatch:
    """Collect the provider's quota-free event list without spending participant credits."""
    response = transport.get(
        f"{BASE_URL}/sports/{provider_sport_key}/events",
        params={"apiKey": api_key, "dateFormat": "iso"},
    )
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Events response must be a list")
    return MultiSportCollectionBatch(
        provider_key=PROVIDER_KEY,
        sport=sport,
        provider_sport_key=provider_sport_key,
        collected_at=response.received_at,
        raw_payload=payload,
        metadata={
            "requests_remaining": _header_int(response.headers, "x-requests-remaining"),
            "requests_used": _header_int(response.headers, "x-requests-used"),
            "request_cost": _header_int(response.headers, "x-requests-last"),
            "endpoint": "events",
        },
    )


def _event_in_pregame_window(
    feed: MultiSportCollectionBatch,
    *,
    as_of: datetime,
    horizon_minutes: int,
) -> bool:
    start = as_of.astimezone(UTC)
    end = start + timedelta(minutes=horizon_minutes)
    for row in feed.raw_payload:
        if str(row.get("sport_key", "")) != feed.provider_sport_key:
            continue
        try:
            commence = parse_timestamp(str(row.get("commence_time", "")))
        except Exception:
            continue
        if start < commence <= end:
            return True
    return False


def _cached_participant_directory(
    connection: sqlite3.Connection,
    *,
    sport: str,
    provider_sport_key: str,
) -> ParticipantDirectory | None:
    rows = connection.execute(
        """
        SELECT source_participant_id, full_name, participant_type
        FROM multisport_participants
        WHERE sport=? AND active_status='provider_listed'
        ORDER BY full_name
        """,
        (sport,),
    ).fetchall()
    if not rows:
        return None
    participants = tuple(
        MultiSportParticipant(
            sport=sport,
            provider_sport_key=provider_sport_key,
            source_participant_id=str(row["source_participant_id"]),
            full_name=str(row["full_name"]),
            participant_type=str(row["participant_type"]),
        )
        for row in rows
    )
    try:
        return ParticipantDirectory(participants)
    except ValueError:
        return None


def _participant_cache_fresh(
    connection: sqlite3.Connection,
    *,
    sport: str,
    as_of: datetime,
    refresh_hours: int,
) -> bool:
    row = connection.execute(
        "SELECT MAX(last_seen_at) FROM multisport_participants WHERE sport=?",
        (sport,),
    ).fetchone()
    if row is None or row[0] is None:
        return False
    try:
        latest = datetime.fromisoformat(str(row[0])).astimezone(UTC)
    except (ValueError, TypeError):
        return False
    return latest >= as_of.astimezone(UTC) - timedelta(hours=refresh_hours)


def _normalize_and_store_events(
    connection: sqlite3.Connection,
    *,
    feed: MultiSportCollectionBatch,
    directory: ParticipantDirectory,
) -> MultiSportCollectionBatch:
    events = parse_events(
        feed.raw_payload,
        sport=feed.sport,
        provider_sport_key=feed.provider_sport_key,
        participants=directory,
    )
    event_batch = MultiSportCollectionBatch(
        provider_key=feed.provider_key,
        sport=feed.sport,
        provider_sport_key=feed.provider_sport_key,
        collected_at=feed.collected_at,
        raw_payload=feed.raw_payload,
        events=events,
        metadata=feed.metadata,
    )
    store_raw_ingest(connection, event_batch, endpoint_name="events")
    store_events(connection, event_batch)
    return event_batch


def collect_sport(
    connection: sqlite3.Connection,
    *,
    settings: Settings,
    sport: str,
    as_of: datetime,
    collector: MultiSportOddsCollector | None = None,
    catalog: SportsCatalogBatch | None = None,
    transport: HttpTransport | None = None,
    budget: DailyCreditBudget | None = None,
) -> dict[str, object]:
    """Collect a sport with free discovery first and cost-bearing calls only near start time."""
    if settings.bot_mode not in {"test_only", "research"}:
        raise RuntimeError("Unsupported research mode")
    api_key = settings.require_odds_api_key()
    transport = transport or _transport(settings)
    collector = collector or MultiSportOddsCollector(
        api_key=api_key,
        transport=transport,
        bookmakers=settings.market_consensus_bookmakers or settings.accessible_bookmakers,
        regions=("us",),
    )
    catalog = catalog or collector.collect_sports_catalog()
    active_keys = discover_active_provider_keys(catalog.raw_payload, sport)
    budget = budget or DailyCreditBudget.from_metadata(
        connection,
        provider_key=PROVIDER_KEY,
        metadata=catalog.metadata,
        as_of=as_of,
        reserve_credits=settings.odds_api_min_remaining_credits,
        daily_credit_cap=settings.odds_api_daily_credit_cap,
    )
    quota_ok = budget.available_today > 0
    remaining = budget.current_remaining
    counts = {
        "participants": 0,
        "events": 0,
        "quotes": 0,
        "odds_research": 0,
        "provider_keys_checked": len(active_keys),
        "provider_keys_in_window": 0,
        "paid_calls_throttled": 0,
        "paid_calls_budget_blocked": 0,
    }
    errors: list[str] = []
    notes: list[str] = []

    if not quota_ok:
        notes.append(
            "Cost-bearing collection skipped because today's sustainable Odds API "
            "allowance is exhausted or remaining-credit metadata is unavailable."
        )

    for provider_sport_key in active_keys:
        try:
            feed = _collect_free_event_feed(
                api_key=api_key,
                transport=transport,
                sport=sport,
                provider_sport_key=provider_sport_key,
            )
        except Exception as exc:
            errors.append(f"{provider_sport_key} events: {exc}")
            continue

        if not _event_in_pregame_window(
            feed,
            as_of=as_of,
            horizon_minutes=settings.multisport_odds_horizon_minutes,
        ):
            continue
        counts["provider_keys_in_window"] += 1
        if budget.available_today <= 0:
            counts["paid_calls_budget_blocked"] += 1
            continue

        directory = None
        cache_fresh = _participant_cache_fresh(
            connection,
            sport=sport,
            as_of=as_of,
            refresh_hours=settings.multisport_participant_refresh_hours,
        )
        if cache_fresh:
            directory = _cached_participant_directory(
                connection,
                sport=sport,
                provider_sport_key=provider_sport_key,
            )
            if directory is not None:
                try:
                    event_batch = _normalize_and_store_events(
                        connection,
                        feed=feed,
                        directory=directory,
                    )
                except ValueError:
                    # A new player/team is a normal reason to refresh the provider whitelist.
                    directory = None

        if directory is None:
            if not budget.reserve(PARTICIPANT_REQUEST_COST):
                counts["paid_calls_budget_blocked"] += 1
                notes.append(
                    f"{provider_sport_key} participant refresh deferred by the daily credit budget."
                )
                continue
            try:
                participant_batch = collector.collect_participants(
                    sport=sport,
                    provider_sport_key=provider_sport_key,
                )
                budget.reconcile(participant_batch.metadata)
                record_poll(
                    connection,
                    provider_key=PROVIDER_KEY,
                    sport=sport,
                    endpoint_name="participants",
                    scope_key=provider_sport_key,
                    polled_at=participant_batch.collected_at,
                    request_cost=participant_batch.metadata.get("request_cost"),
                    requests_remaining=participant_batch.metadata.get("requests_remaining"),
                )
                store_raw_ingest(connection, participant_batch, endpoint_name="participants")
                counts["participants"] += store_participants(connection, participant_batch)
                directory = ParticipantDirectory(participant_batch.participants)
                event_batch = _normalize_and_store_events(
                    connection,
                    feed=feed,
                    directory=directory,
                )
            except Exception as exc:
                errors.append(f"{provider_sport_key} participants/events: {exc}")
                continue

        counts["events"] += len(event_batch.events)
        if not poll_due(
            connection,
            provider_key=PROVIDER_KEY,
            sport=sport,
            endpoint_name="odds",
            scope_key=provider_sport_key,
            as_of=as_of,
            interval_minutes=settings.multisport_odds_refresh_minutes,
        ):
            counts["paid_calls_throttled"] += 1
            continue
        if not budget.reserve(ODDS_REQUEST_COST):
            counts["paid_calls_budget_blocked"] += 1
            continue
        try:
            odds_batch = collector.collect_odds(
                sport=sport,
                provider_sport_key=provider_sport_key,
                participants=directory,
            )
            budget.reconcile(odds_batch.metadata)
            record_poll(
                connection,
                provider_key=PROVIDER_KEY,
                sport=sport,
                endpoint_name="odds",
                scope_key=provider_sport_key,
                polled_at=odds_batch.collected_at,
                request_cost=odds_batch.metadata.get("request_cost"),
                requests_remaining=odds_batch.metadata.get("requests_remaining"),
            )
            counts["quotes"] += store_odds(connection, odds_batch)
            future_event_ids = connection.execute(
                """
                SELECT event_id FROM multisport_events
                WHERE sport=? AND provider_sport_key=? AND commence_time>?
                  AND commence_time<=?
                """,
                (
                    sport,
                    provider_sport_key,
                    as_of.astimezone(UTC).isoformat(),
                    (as_of.astimezone(UTC) + timedelta(minutes=settings.multisport_odds_horizon_minutes)).isoformat(),
                ),
            ).fetchall()
            for row in future_event_ids:
                try:
                    mark_current_odds_research(
                        connection,
                        event_id=str(row["event_id"]),
                        as_of=odds_batch.collected_at,
                        max_age_minutes=settings.odds_stale_after_minutes,
                    )
                    counts["odds_research"] += 1
                except Exception as exc:
                    errors.append(f"odds research {row['event_id']}: {exc}")
        except Exception as exc:
            errors.append(f"{provider_sport_key} odds: {exc}")

    connection.commit()
    return {
        "sport": sport,
        "active_provider_keys": active_keys,
        "quota_remaining_at_catalog": remaining,
        "quota_budget": budget.as_dict(),
        **counts,
        "notes": tuple(notes),
        "errors": tuple(errors),
    }


def _score_poll_due(
    connection: sqlite3.Connection,
    *,
    sport: str,
    as_of: datetime,
) -> bool:
    grace = SCORE_GRACE_MINUTES[sport]
    cutoff = as_of.astimezone(UTC) - timedelta(minutes=grace)
    row = connection.execute(
        """
        SELECT 1
        FROM multisport_paper_decisions d
        JOIN multisport_events e ON e.event_id=d.event_id
        LEFT JOIN multisport_paper_results pr ON pr.paper_decision_id=d.paper_decision_id
        LEFT JOIN multisport_results r ON r.event_id=d.event_id
        WHERE d.sport=? AND pr.paper_decision_id IS NULL AND r.event_id IS NULL
          AND e.commence_time<=?
        LIMIT 1
        """,
        (sport, cutoff.isoformat()),
    ).fetchone()
    return row is not None


def collect_results(
    connection: sqlite3.Connection,
    *,
    settings: Settings,
    sport: str,
    as_of: datetime,
    budget: DailyCreditBudget | None = None,
    catalog: SportsCatalogBatch | None = None,
) -> dict[str, object]:
    if sport not in AUTO_SCORE_SPORTS | {"mma"}:
        return {"sport": sport, "stored": 0, "skipped": 0, "note": "no approved result adapter"}
    if not _score_poll_due(connection, sport=sport, as_of=as_of):
        return {
            "sport": sport,
            "stored": 0,
            "skipped": 0,
            "note": "result API not called: no unsettled paper decision is old enough to be final",
        }

    if sport == "mma":
        key = settings.api_sports_key
        if not key:
            return {
                "sport": sport,
                "stored": 0,
                "skipped": 0,
                "errors": ("API_SPORTS_KEY is not configured",),
            }
        collector = ApiSportsMmaResultsCollector(api_key=key, transport=_transport(settings))
        rows = connection.execute(
            """
            SELECT DISTINCT e.event_id, e.commence_time,
                   home.full_name AS home_name,
                   away.full_name AS away_name
            FROM multisport_paper_decisions d
            JOIN multisport_events e ON e.event_id=d.event_id
            JOIN multisport_participants home
              ON home.participant_id=e.home_participant_id
            JOIN multisport_participants away
              ON away.participant_id=e.away_participant_id
            LEFT JOIN multisport_paper_results pr
              ON pr.paper_decision_id=d.paper_decision_id
            LEFT JOIN multisport_results r ON r.event_id=e.event_id
            WHERE d.sport=? AND pr.paper_decision_id IS NULL AND r.event_id IS NULL
              AND e.commence_time<=?
            ORDER BY e.commence_time
            """,
            (
                sport,
                (as_of.astimezone(UTC) - timedelta(minutes=SCORE_GRACE_MINUTES[sport])).isoformat(),
            ),
        ).fetchall()
        stored = skipped = throttled = 0
        errors: list[str] = []
        for row in rows:
            event_id = str(row["event_id"])
            if not poll_due(
                connection,
                provider_key=MMA_RESULT_PROVIDER,
                sport=sport,
                endpoint_name="results",
                scope_key=event_id,
                as_of=as_of,
                interval_minutes=settings.multisport_mma_result_poll_minutes,
            ):
                throttled += 1
                continue
            try:
                final = collector.collect_event(
                    local_home_name=str(row["home_name"]),
                    local_away_name=str(row["away_name"]),
                    commence_time=parse_timestamp(str(row["commence_time"])),
                )
                record_poll(
                    connection,
                    provider_key=MMA_RESULT_PROVIDER,
                    sport=sport,
                    endpoint_name="results",
                    scope_key=event_id,
                    polled_at=final.collected_at if final is not None else as_of,
                    request_cost=1,
                )
                if final is None:
                    skipped += 1
                    continue
                record_result(
                    connection,
                    event_id=event_id,
                    result_status=final.result_status,
                    source_key=MMA_RESULT_PROVIDER,
                    source_updated_at=final.source_updated_at,
                    collected_at=final.collected_at,
                    home_score=final.home_score,
                    away_score=final.away_score,
                    winner_role=final.winner_role,
                    raw_result=final.raw_result,
                )
                stored += 1
            except Exception as exc:
                errors.append(f"{event_id}: {exc}")
        connection.commit()
        return {
            "sport": sport,
            "stored": stored,
            "skipped": skipped,
            "throttled": throttled,
            "errors": tuple(errors),
        }

    key = settings.require_odds_api_key()
    if budget is None:
        quota_collector = MultiSportOddsCollector(
            api_key=key,
            transport=_transport(settings),
            bookmakers=settings.market_consensus_bookmakers or settings.accessible_bookmakers,
            regions=("us",),
        )
        catalog = catalog or quota_collector.collect_sports_catalog()
        budget = DailyCreditBudget.from_metadata(
            connection,
            provider_key=PROVIDER_KEY,
            metadata=catalog.metadata,
            as_of=as_of,
            reserve_credits=settings.odds_api_min_remaining_credits,
            daily_credit_cap=settings.odds_api_daily_credit_cap,
        )
    spec = get_sport_market(sport)
    if spec.exact_provider_sport_keys:
        provider_keys = spec.exact_provider_sport_keys
    else:
        provider_keys = tuple(
            str(row["provider_sport_key"])
            for row in connection.execute(
                """
                SELECT DISTINCT e.provider_sport_key
                FROM multisport_paper_decisions d
                JOIN multisport_events e ON e.event_id=d.event_id
                LEFT JOIN multisport_paper_results pr
                  ON pr.paper_decision_id=d.paper_decision_id
                LEFT JOIN multisport_results r ON r.event_id=e.event_id
                WHERE d.sport=? AND pr.paper_decision_id IS NULL AND r.event_id IS NULL
                  AND e.commence_time<=?
                ORDER BY e.provider_sport_key
                """,
                (
                    sport,
                    (
                        as_of.astimezone(UTC)
                        - timedelta(minutes=SCORE_GRACE_MINUTES[sport])
                    ).isoformat(),
                ),
            ).fetchall()
        )
    collector = MultiSportScoresCollector(api_key=key, transport=_transport(settings))
    stored = skipped = throttled = budget_blocked = 0
    errors: list[str] = []
    for provider_key in provider_keys:
        if not poll_due(
            connection,
            provider_key=PROVIDER_KEY,
            sport=sport,
            endpoint_name="scores",
            scope_key=provider_key,
            as_of=as_of,
            interval_minutes=settings.multisport_score_poll_minutes,
        ):
            throttled += 1
            continue
        if not budget.reserve(SCORE_REQUEST_COST):
            budget_blocked += 1
            continue
        try:
            batch = collector.collect(sport=sport, provider_sport_key=provider_key)
            budget.reconcile(batch.metadata)
            record_poll(
                connection,
                provider_key=PROVIDER_KEY,
                sport=sport,
                endpoint_name="scores",
                scope_key=provider_key,
                polled_at=batch.collected_at,
                request_cost=(batch.metadata or {}).get("request_cost"),
                requests_remaining=(batch.metadata or {}).get("requests_remaining"),
            )
            result = persist_score_batch(connection, batch)
            stored += int(result["stored"])
            skipped += int(result["skipped"])
            errors.extend(result["errors"])
        except Exception as exc:
            errors.append(f"{provider_key}: {exc}")
    connection.commit()
    return {
        "sport": sport,
        "stored": stored,
        "skipped": skipped,
        "throttled": throttled,
        "budget_blocked": budget_blocked,
        "quota_budget": budget.as_dict(),
        "errors": tuple(errors),
    }


def evaluate_ready_events(
    connection: sqlite3.Connection,
    *,
    settings: Settings,
    sport: str,
    as_of: datetime,
    evaluation_window_minutes: int = 90,
) -> dict[str, object]:
    """Create a final test-only/pass decision only when a model and features exist."""
    if not settings.sportsbook_lists_approved():
        return {"sport": sport, "evaluated": 0, "skipped": 0, "errors": ("Sportsbook lists are not approved",)}
    model = latest_model(connection, sport)
    if model is None and sport in MARKET_BASELINE_SPORTS:
        model = ensure_market_baseline_model(connection, sport=sport, as_of=as_of)
    if model is None:
        return {"sport": sport, "evaluated": 0, "skipped": 0, "errors": ("No trained test-only model",)}
    end = as_of.astimezone(UTC) + timedelta(minutes=evaluation_window_minutes)
    events = connection.execute(
        """
        SELECT e.* FROM multisport_events e
        LEFT JOIN multisport_paper_decisions d
          ON d.event_id=e.event_id AND d.market_key=? AND d.period=?
        WHERE e.sport=? AND e.status='scheduled' AND e.commence_time>? AND e.commence_time<=?
          AND d.paper_decision_id IS NULL
        ORDER BY e.commence_time
        """,
        (
            model["market_key"], model["period"], sport,
            as_of.astimezone(UTC).isoformat(), end.isoformat(),
        ),
    ).fetchall()
    evaluated = skipped = 0
    decisions: list[dict[str, object]] = []
    errors: list[str] = []
    for event in events:
        event_id = str(event["event_id"])
        try:
            if sport == "epl":
                record_epl_baseline_live_features(
                    connection,
                    event_id=event_id,
                    cutoff=as_of,
                )
            # Research is checked by the evaluation engine. Calling the gate here makes
            # the blocker visible in logs without allowing missing research to become data.
            research_gate(
                connection,
                event_id=event_id,
                sport=sport,
                as_of=as_of,
                odds_stale_after_minutes=settings.odds_stale_after_minutes,
            )
            if str(model["feature_version"]) == BASELINE_FEATURE_VERSION:
                prediction_id = create_market_baseline_prediction(
                    connection,
                    event_id=event_id,
                    sport=sport,
                    as_of=as_of,
                    stale_after_minutes=settings.odds_stale_after_minutes,
                )
            else:
                prediction_id = predict_event(
                    connection,
                    event_id=event_id,
                    model_version=str(model["model_version"]),
                    prediction_time=as_of,
                    feature_cutoff=as_of,
                )
            result = evaluate_event(
                connection,
                event_id=event_id,
                prediction_id=prediction_id,
                as_of=as_of,
                accessible_bookmakers=settings.accessible_bookmakers,
                consensus_bookmakers=settings.market_consensus_bookmakers,
                stale_after_minutes=settings.odds_stale_after_minutes,
                capture_unqualified_test=False,
            )
            decisions.append(dict(result.__dict__))
            evaluated += 1
        except Exception as exc:
            skipped += 1
            errors.append(f"{event_id}: {exc}")
    return {
        "sport": sport,
        "evaluated": evaluated,
        "skipped": skipped,
        "decisions": tuple(decisions),
        "errors": tuple(errors),
    }


def _cycle_report_time(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    sports: Iterable[str],
) -> datetime:
    """Advance the report cutoff through odds collected during this cycle.

    HTTP responses are timestamped when they are received, which can be a few seconds
    after ``started_at``.  Readiness must include those records in the same cycle
    instead of hiding them as future evidence until the next run.
    """
    start = started_at.astimezone(UTC)
    selected_sports = tuple(dict.fromkeys(str(sport).casefold().strip() for sport in sports))
    if not selected_sports:
        return start
    row = connection.execute(
        f"""
        SELECT collected_at
        FROM multisport_research_checks
        WHERE category='current_odds' AND source_key='the_odds_api'
          AND collected_at>=?
          AND sport IN ({','.join('?' for _ in selected_sports)})
        ORDER BY collected_at DESC
        LIMIT 1
        """,
        (start.isoformat(), *selected_sports),
    ).fetchone()
    if row is None:
        return start
    latest = parse_timestamp(str(row["collected_at"]))
    return max(start, latest)


def run_cycle(
    connection: sqlite3.Connection,
    *,
    settings: Settings,
    sports: Iterable[str] = ALL_SPORTS,
    as_of: datetime | None = None,
) -> dict[str, object]:
    """One safe automated paper cycle: free discovery -> gated odds -> results -> evaluate -> settle."""
    now = as_of or datetime.now(UTC)
    bootstrap(connection, as_of=now)
    api_key = settings.require_odds_api_key()
    transport = _transport(settings)
    collector = MultiSportOddsCollector(
        api_key=api_key,
        transport=transport,
        bookmakers=settings.market_consensus_bookmakers or settings.accessible_bookmakers,
        regions=("us",),
    )
    catalog = collector.collect_sports_catalog()
    budget = DailyCreditBudget.from_metadata(
        connection,
        provider_key=PROVIDER_KEY,
        metadata=catalog.metadata,
        as_of=now,
        reserve_credits=settings.odds_api_min_remaining_credits,
        daily_credit_cap=settings.odds_api_daily_credit_cap,
    )
    selected_sports = tuple(dict.fromkeys(str(sport).casefold().strip() for sport in sports))
    histories = history_status(connection)
    training_sports: dict[str, object] = {}
    for key in selected_sports:
        if key not in ALL_SPORTS:
            raise ValueError(f"Unsupported cycle sport: {key}")
        model = latest_model(connection, key)
        count = int(histories[key])
        training_sports[key] = {
            "historical_training_examples": count,
            "training_minimum": MIN_TRAINING_EXAMPLES,
            "examples_needed": max(0, MIN_TRAINING_EXAMPLES - count),
            "training_ready": count >= MIN_TRAINING_EXAMPLES,
            "model_version": str(model["model_version"]) if model else None,
            "model_status": str(model["status"]) if model else "not_trained",
        }
    output: dict[str, object] = {
        "as_of": now.astimezone(UTC).isoformat(),
        "training": {
            "minimum_examples_per_sport": MIN_TRAINING_EXAMPLES,
            "all_selected_models_trained": all(
                row["model_status"] != "not_trained" for row in training_sports.values()
            ),
            "sports": training_sports,
        },
        "sports": {},
        "quota_budget": budget.as_dict(),
    }
    for key in selected_sports:
        output["sports"][key] = {}

    # Final results get first use of each day's credits so Telegram settlement is not
    # displaced by another pregame odds refresh. Tennis is checked first because one
    # tour can expose several tournament-specific provider keys.
    result_order = tuple(
        key for key in ("atp", "wta", "nba", "wnba", "epl", "mma") if key in selected_sports
    )
    for key in result_order:
        output["sports"][key]["results"] = collect_results(
            connection,
            settings=settings,
            sport=key,
            as_of=now,
            budget=budget,
            catalog=catalog,
        )

    # Rotate the first pregame sport each day so one league cannot consume every
    # remaining paid call simply because it appears first in the registry.
    rotation = now.toordinal() % len(selected_sports) if selected_sports else 0
    collection_order = selected_sports[rotation:] + selected_sports[:rotation]
    for key in collection_order:
        collection = collect_sport(
            connection,
            settings=settings,
            sport=key,
            as_of=now,
            collector=collector,
            catalog=catalog,
            transport=transport,
            budget=budget,
        )
        output["sports"][key]["collection"] = collection
    report_time = _cycle_report_time(
        connection,
        started_at=now,
        sports=selected_sports,
    )
    output["as_of"] = report_time.isoformat()
    output["quota_budget"] = budget.as_dict()
    output["live_research"] = collect_automatic_live_research(
        connection,
        sports=selected_sports,
        as_of=report_time,
        feed_urls=settings.multisport_research_feed_urls,
        bearer_token=settings.multisport_research_feed_token,
        transport=transport,
        horizon_minutes=settings.multisport_research_horizon_minutes,
        max_requests=settings.multisport_research_max_requests,
    )
    for key in selected_sports:
        output["sports"][key]["evaluation"] = evaluate_ready_events(
            connection,
            settings=settings,
            sport=key,
            as_of=report_time,
        )
    output["settlement"] = settle_ready_decisions(connection, settled_at=report_time)
    return output


def _current_paper_selection(
    connection: sqlite3.Connection,
    *,
    report_time: datetime,
    horizon_minutes: int,
    stale_after_minutes: int,
    sport: str | None = None,
) -> sqlite3.Row | None:
    """Return only a still-actionable selection from the current reporting window."""
    if horizon_minutes <= 0:
        raise ValueError("Selection horizon must be greater than zero minutes")
    if stale_after_minutes <= 0:
        raise ValueError("Selection freshness must be greater than zero minutes")

    fresh_after = report_time - timedelta(minutes=stale_after_minutes)
    horizon_end = report_time + timedelta(minutes=horizon_minutes)
    sport_clause = "AND d.sport=?" if sport is not None else ""
    parameters: list[object] = [
        fresh_after.isoformat(),
        report_time.isoformat(),
        report_time.isoformat(),
        horizon_end.isoformat(),
        fresh_after.isoformat(),
        report_time.isoformat(),
    ]
    if sport is not None:
        parameters.append(sport)

    return connection.execute(
        f"""
        SELECT d.*, selected.full_name AS selection_name,
               e.commence_time AS event_commence_time,
               e.status AS event_status,
               o.collected_at AS odds_collected_at,
               o.market_status AS odds_market_status,
               0 AS selection_settled
        FROM multisport_paper_decisions d
        JOIN multisport_events e ON e.event_id = d.event_id
        JOIN multisport_odds_snapshots o ON o.odds_snapshot_id = d.odds_snapshot_id
        LEFT JOIN multisport_participants selected
          ON selected.participant_id = d.outcome_participant_id
        LEFT JOIN multisport_paper_results settled
          ON settled.paper_decision_id = d.paper_decision_id
        WHERE d.decision_type IN ('official_pick','conditional_pick','test_only_selection')
          AND d.check_time>=? AND d.check_time<=?
          AND e.status='scheduled'
          AND e.commence_time>? AND e.commence_time<=?
          AND o.market_status='open'
          AND o.collected_at>=? AND o.collected_at<=?
          AND settled.paper_decision_id IS NULL
          {sport_clause}
        ORDER BY d.check_time DESC, d.pick_strength_score DESC,
                 d.ev_per_dollar DESC, d.probability_edge DESC
        LIMIT 1
        """,
        tuple(parameters),
    ).fetchone()


def _current_selection_display(
    row: dict[str, object],
    *,
    paper_unit_dollars: float,
) -> dict[str, object]:
    display = decision_presentation(row, paper_unit_dollars=paper_unit_dollars)
    for key in (
        "check_time",
        "event_commence_time",
        "event_status",
        "odds_collected_at",
        "odds_market_status",
        "selection_settled",
    ):
        display[key] = row.get(key)
    return display


def status_report(
    connection: sqlite3.Connection,
    *,
    paper_unit_dollars: float = 10.0,
    as_of: datetime | None = None,
    selection_horizon_minutes: int = 90,
    odds_stale_after_minutes: int = 60,
) -> dict[str, object]:
    ensure_runtime_schema(connection)
    report_time = (as_of or datetime.now(UTC)).astimezone(UTC)
    histories = history_status(connection)
    sports: dict[str, object] = {}
    for sport in ALL_SPORTS:
        spec = get_sport_market(sport)
        validation = connection.execute(
            "SELECT * FROM multisport_validation_metrics WHERE validation_bucket=?",
            (validation_bucket(sport),),
        ).fetchone()
        model = latest_model(connection, sport)
        settlement_rows = connection.execute(
            """
            SELECT sportsbook_key, rule_status, rule_key
            FROM multisport_settlement_rules
            WHERE sport=? AND market_key=? AND period=?
            ORDER BY sportsbook_key
            """,
            (sport, spec.internal_market_key, spec.period),
        ).fetchall()
        verified_books = tuple(
            str(row["sportsbook_key"])
            for row in settlement_rows
            if str(row["rule_status"]) == "verified"
        )
        pending_books = tuple(
            str(row["sportsbook_key"])
            for row in settlement_rows
            if str(row["rule_status"]) == "pending"
        )
        next_event = connection.execute(
            """
            SELECT event_id, commence_time
            FROM multisport_events
            WHERE sport=? AND status='scheduled' AND commence_time>?
            ORDER BY commence_time
            LIMIT 1
            """,
            (sport, report_time.isoformat()),
        ).fetchone()
        next_readiness = None
        if next_event is not None:
            next_readiness = live_readiness_report(
                connection,
                event_id=str(next_event["event_id"]),
                sport=sport,
                as_of=report_time,
            ).as_dict()
        sport_best = _current_paper_selection(
            connection,
            report_time=report_time,
            horizon_minutes=selection_horizon_minutes,
            stale_after_minutes=odds_stale_after_minutes,
            sport=sport,
        )
        sport_best_row = dict(sport_best) if sport_best else None
        sports[sport] = {
            "market": spec.internal_market_key,
            "period": spec.period,
            "historical_training_examples": histories[sport],
            "training_minimum": 50,
            "validation_target": spec.validation_target,
            "completed_oos": int(validation["completed_count"]) if validation else 0,
            "validation_review": str(validation["review_status"]) if validation else "collecting",
            "calibration_status": str(validation["calibration_status"]) if validation else "untested",
            "clv_status": str(validation["clv_status"]) if validation else "unknown",
            "model_version": str(model["model_version"]) if model else None,
            "model_status": str(model["status"]) if model else "not_trained",
            "verified_settlement_bookmakers": verified_books,
            "pending_settlement_bookmakers": pending_books,
            "bookmaker_independence": True,
            "live_research_policy": research_policy_summary(sport),
            "next_event_live_readiness": next_readiness,
            "paper_decisions": int(connection.execute(
                "SELECT COUNT(*) FROM multisport_paper_decisions WHERE sport=?", (sport,)
            ).fetchone()[0]),
            "best_current_paper_selection": sport_best_row,
            "best_current_paper_selection_display": (
                _current_selection_display(
                    sport_best_row,
                    paper_unit_dollars=paper_unit_dollars,
                )
                if sport_best_row else None
            ),
        }
    best = _current_paper_selection(
        connection,
        report_time=report_time,
        horizon_minutes=selection_horizon_minutes,
        stale_after_minutes=odds_stale_after_minutes,
    )
    best_row = dict(best) if best else None
    return {
        "generated_at": report_time.isoformat(),
        "selection_horizon_minutes": selection_horizon_minutes,
        "odds_stale_after_minutes": odds_stale_after_minutes,
        "sports": sports,
        "best_current_paper_selection": best_row,
        "best_current_paper_selection_display": (
            _current_selection_display(best_row, paper_unit_dollars=paper_unit_dollars)
            if best_row else None
        ),
        "paper_unit_dollars": round(paper_unit_dollars, 2),
        "automatic_wagering": False,
        "manual_approval_required": True,
        "protected_existing_research_paths": ("nfl", "mlb"),
    }

