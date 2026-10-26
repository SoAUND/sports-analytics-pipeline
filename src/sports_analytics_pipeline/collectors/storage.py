"""Transactional storage for normalized collector results and original records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any, Iterable

from sports_analytics_pipeline.calculations import decimal_to_american

from .base import CollectionBatch, CollectedSpreadQuote
from .nflverse import (
    ArchiveBatch,
    InjuryRecord,
    NflverseBatch,
    RosterRecord,
    ScheduleGame,
)
from .nws import WeatherBatch
from .official_nfl import OfficialPageCheck
from .mlb_official import (
    MlbRecentResultsBatch,
    MlbScheduleBatch,
    MlbTransactionsBatch,
    OfficialMlbPageCheck,
)
from .official_schedule import OfficialScheduleBatch


ODDS_KICKOFF_MATCH_TOLERANCE = timedelta(minutes=30)


def iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Stored timestamps must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _json_default(value: Any):
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


class CollectorRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.db = connection

    def begin_run(self, source_key: str, endpoint_name: str, started_at: datetime) -> str:
        run_id = str(uuid.uuid4())
        cursor = self.db.execute(
            """
            INSERT INTO collection_runs (
                collection_run_id, source_key, endpoint_name, started_at, status
            ) VALUES (?, ?, ?, ?, 'running')
            """,
            (run_id, source_key, endpoint_name, iso(started_at)),
        )
        self.db.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        finished_at: datetime,
        status: str,
        received: int = 0,
        stored: int = 0,
        warnings: Iterable[str] = (),
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in {"succeeded", "partial", "failed", "skipped"}:
            raise ValueError("Invalid completed collection status")
        metadata = metadata or {}
        cursor = self.db.execute(
            """
            UPDATE collection_runs
            SET finished_at = ?, status = ?, records_received = ?, records_stored = ?,
                warnings_json = ?, error_message = ?, requests_remaining = ?,
                requests_used = ?, request_cost = ?
            WHERE collection_run_id = ? AND status = 'running'
            """,
            (
                iso(finished_at),
                status,
                received,
                stored,
                canonical_json(tuple(warnings)),
                error_message,
                metadata.get("requests_remaining"),
                metadata.get("requests_used"),
                metadata.get("request_cost"),
                run_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("Unknown or already completed collection run")
        self.db.commit()

    def store_raw(
        self,
        *,
        source_key: str,
        endpoint_name: str,
        payload: Any,
        collected_at: datetime,
        source_updated_at: datetime | None = None,
        http_status: int | None = 200,
    ) -> int:
        payload_json = canonical_json(payload)
        checksum = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        self.db.execute(
            """
            INSERT OR IGNORE INTO raw_ingests (
                source_key, endpoint_name, source_updated_at, collected_at,
                content_sha256, payload_json, http_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_key,
                endpoint_name,
                iso(source_updated_at) if source_updated_at else None,
                iso(collected_at),
                checksum,
                payload_json,
                http_status,
            ),
        )
        row = self.db.execute(
            """
            SELECT ingest_id FROM raw_ingests
            WHERE source_key = ? AND endpoint_name = ? AND content_sha256 = ?
            """,
            (source_key, endpoint_name, checksum),
        ).fetchone()
        return int(row[0])

    def latest_raw_ingest(self, source_key: str, endpoint_name: str):
        """Return the newest preserved response for safe local replay."""
        return self.db.execute(
            """
            SELECT ingest_id, collected_at, payload_json
            FROM raw_ingests
            WHERE source_key = ? AND endpoint_name = ?
            ORDER BY collected_at DESC, ingest_id DESC
            LIMIT 1
            """,
            (source_key, endpoint_name),
        ).fetchone()

    def persist_schedules(self, batch: NflverseBatch) -> int:
        self.store_raw(
            source_key="nflverse",
            endpoint_name=f"schedules_{batch.season}",
            payload=batch.raw_text,
            collected_at=batch.collected_at,
            source_updated_at=batch.source_updated_at,
        )
        return self._persist_schedule_records(
            batch.records, source_key="nflverse", collected_at=batch.collected_at
        )

    def persist_official_schedules(self, batch: OfficialScheduleBatch) -> int:
        self.store_raw(
            source_key="nfl_schedule",
            endpoint_name=f"preseason_{batch.season}",
            payload=batch.raw_payload,
            collected_at=batch.collected_at,
        )
        return self._persist_schedule_records(
            batch.records, source_key="nfl_schedule", collected_at=batch.collected_at
        )

    def _persist_schedule_records(
        self,
        records: Iterable[ScheduleGame],
        *,
        source_key: str,
        collected_at: datetime,
    ) -> int:
        stored = 0
        with self.db:
            for item in records:
                if not isinstance(item, ScheduleGame):
                    raise TypeError("Schedule batch contains a non-schedule record")
                existing = self.db.execute(
                    "SELECT * FROM games WHERE game_id = ?", (item.source_game_id,)
                ).fetchone()
                if existing and (
                    existing["season"] != item.season
                    or existing["season_type"] != item.season_type
                    or existing["week"] != item.week
                    or existing["home_team_id"] != item.home_team_id
                    or existing["away_team_id"] != item.away_team_id
                ):
                    raise ValueError(
                        f"Schedule identity changed for game {item.source_game_id}; manual review required"
                    )
                identity = self.db.execute(
                    """
                    SELECT game_id FROM games
                    WHERE season = ? AND season_type = ? AND week = ?
                      AND home_team_id = ? AND away_team_id = ? AND kickoff_time = ?
                    """,
                    (
                        item.season,
                        item.season_type,
                        item.week,
                        item.home_team_id,
                        item.away_team_id,
                        iso(item.kickoff_time),
                    ),
                ).fetchone()
                game_id = str(identity[0]) if identity else item.source_game_id
                mapped = self.db.execute(
                    """
                    SELECT game_id FROM game_source_ids
                    WHERE source_key = ? AND source_game_id = ?
                    """,
                    (source_key, item.source_game_id),
                ).fetchone()
                if mapped and str(mapped[0]) != game_id:
                    raise ValueError(
                        f"Source mapping changed for game {item.source_game_id}; manual review required"
                    )
                now = iso(collected_at)
                self.db.execute(
                    """
                    INSERT INTO games (
                        game_id, source_game_id, season, season_type, week,
                        home_team_id, away_team_id, kickoff_time, stadium_id,
                        surface_override, roof_status, neutral_site, status,
                        home_score, away_score, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        kickoff_time = excluded.kickoff_time,
                        stadium_id = excluded.stadium_id,
                        surface_override = excluded.surface_override,
                        roof_status = excluded.roof_status,
                        neutral_site = excluded.neutral_site,
                        status = excluded.status,
                        home_score = excluded.home_score,
                        away_score = excluded.away_score,
                        updated_at = excluded.updated_at
                    """,
                    (
                        game_id,
                        item.source_game_id,
                        item.season,
                        item.season_type,
                        item.week,
                        item.home_team_id,
                        item.away_team_id,
                        iso(item.kickoff_time),
                        item.stadium_id,
                        item.surface_override,
                        item.roof_status,
                        int(item.neutral_site),
                        item.status,
                        item.home_score,
                        item.away_score,
                        now,
                        now,
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO game_source_ids (
                        source_key, source_game_id, game_id, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source_key, source_game_id) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    (source_key, item.source_game_id, game_id, now, now),
                )
                stored += 1
        return stored

    def _match_game(self, quote, source_key: str) -> str:
        mapped = self.db.execute(
            """
            SELECT game_id FROM game_source_ids
            WHERE source_key = ? AND source_game_id = ?
            """,
            (source_key, quote.source_event_id),
        ).fetchone()
        if mapped:
            return str(mapped[0])

        candidates = self.db.execute(
            """
            SELECT game_id, kickoff_time FROM games
            WHERE sport = ? AND home_team_id = ? AND away_team_id = ?
            """,
            (getattr(quote, "sport", "nfl"), quote.home_team_id, quote.away_team_id),
        ).fetchall()
        matches = []
        for row in candidates:
            kickoff = datetime.fromisoformat(row["kickoff_time"])
            if abs(kickoff - quote.commence_time) <= ODDS_KICKOFF_MATCH_TOLERANCE:
                matches.append(str(row["game_id"]))
        if len(matches) != 1:
            raise ValueError(
                f"Odds event {quote.source_event_id} matched {len(matches)} games; schedule review required"
            )
        self.db.execute(
            """
            INSERT INTO game_source_ids (
                source_key, source_game_id, game_id, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                source_key,
                quote.source_event_id,
                matches[0],
                iso(quote.collected_at),
                iso(quote.collected_at),
            ),
        )
        return matches[0]

    def persist_odds(
        self,
        batch: CollectionBatch,
        *,
        accessible_bookmakers: Iterable[str] = (),
        consensus_bookmakers: Iterable[str] = (),
        stale_after_minutes: int = 60,
    ) -> tuple[int, tuple[str, ...]]:
        if batch.provider_key not in {"the_odds_api", "therundown"}:
            raise ValueError(f"Unsupported odds provider: {batch.provider_key}")
        ingest_id = self.store_raw(
            source_key=batch.provider_key,
            endpoint_name=str(batch.metadata.get("endpoint", "current_odds")),
            payload=batch.raw_payload,
            collected_at=batch.collected_at,
        )
        accessible = set(accessible_bookmakers)
        consensus = set(consensus_bookmakers)
        warnings = list(batch.warnings)
        stored = 0
        current_endpoint = batch.metadata.get("endpoint") in {
            "current_odds",
            "current_preseason_odds",
            "current_mlb_odds",
        }
        with self.db:
            for quote in batch.quotes:
                try:
                    game_id = self._match_game(quote, batch.provider_key)
                except ValueError as exc:
                    warnings.append(str(exc))
                    continue
                is_stale = current_endpoint and (
                    quote.collected_at - quote.source_updated_at
                ) > timedelta(minutes=stale_after_minutes)
                status = "stale" if is_stale else quote.status
                self.db.execute(
                    """
                    INSERT INTO sportsbooks (
                        sportsbook_key, display_name, consensus_eligible, accessible
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(sportsbook_key) DO UPDATE SET
                        display_name = excluded.display_name,
                        consensus_eligible = excluded.consensus_eligible,
                        accessible = excluded.accessible
                    """,
                    (
                        quote.bookmaker_key,
                        quote.bookmaker_title,
                        int(quote.bookmaker_key in consensus),
                        int(quote.bookmaker_key in accessible),
                    ),
                )
                opponent = (
                    quote.away_team_id
                    if quote.outcome_team_id == quote.home_team_id
                    else quote.home_team_id
                )
                cursor = self.db.execute(
                    """
                    INSERT OR IGNORE INTO odds_snapshots (
                        game_id, ingest_id, sportsbook_key, market_key, period,
                        settlement_rule, outcome_team_id, opponent_team_id,
                        spread_points, decimal_odds, american_odds,
                        source_updated_at, collected_at, market_status,
                        is_accessible, is_consensus_eligible
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        ingest_id,
                        quote.bookmaker_key,
                        quote.market_key,
                        quote.period,
                        quote.settlement_rule,
                        quote.outcome_team_id,
                        opponent,
                        None if quote.points is None else float(quote.points),
                        float(quote.decimal_odds),
                        float(decimal_to_american(quote.decimal_odds)),
                        iso(quote.source_updated_at),
                        iso(quote.collected_at),
                        status,
                        int(quote.bookmaker_key in accessible),
                        int(quote.bookmaker_key in consensus),
                    ),
                )
                stored += int(cursor.rowcount > 0)
        return stored, tuple(warnings)


    def persist_mlb_schedule(self, batch: MlbScheduleBatch) -> int:
        """Persist official MLB games and probable pitchers without fabricating confirmations."""
        ingest_id = self.store_raw(
            source_key="mlb_schedule",
            endpoint_name=f"schedule_{batch.schedule_date}",
            payload=batch.raw_payload,
            collected_at=batch.collected_at,
        )
        stored = 0
        now = iso(batch.collected_at)
        with self.db:
            for item in batch.games:
                game_id = f"mlb_{item.source_game_id}"
                self.db.execute(
                    """
                    INSERT INTO games (
                        game_id, source_game_id, sport, season, season_type, week,
                        home_team_id, away_team_id, kickoff_time, stadium_id,
                        neutral_site, status, home_score, away_score, created_at, updated_at
                    ) VALUES (?, ?, 'mlb', ?, 'regular', 0, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        kickoff_time=excluded.kickoff_time,
                        stadium_id=excluded.stadium_id,
                        status=excluded.status,
                        home_score=COALESCE(excluded.home_score, games.home_score),
                        away_score=COALESCE(excluded.away_score, games.away_score),
                        updated_at=excluded.updated_at
                    """,
                    (
                        game_id, item.source_game_id, item.season,
                        item.home_team_id, item.away_team_id, iso(item.game_time),
                        item.stadium_id, item.status, item.home_score, item.away_score, now, now,
                    ),
                )
                self.db.execute(
                    """
                    INSERT INTO game_source_ids (
                        source_key, source_game_id, game_id, first_seen_at, last_seen_at
                    ) VALUES ('mlb_schedule', ?, ?, ?, ?)
                    ON CONFLICT(source_key, source_game_id) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at
                    """,
                    (item.source_game_id, game_id, now, now),
                )
                for pitcher in (item.probable_home_pitcher, item.probable_away_pitcher):
                    if pitcher is None:
                        continue
                    self.db.execute(
                        """
                        INSERT INTO players (
                            player_id, sport, player_name, primary_team_id, position,
                            roster_status, verified_on, source_key, source_url
                        ) VALUES (?, 'mlb', ?, ?, 'P', 'probable', ?, 'mlb_probable_pitchers', ?)
                        ON CONFLICT(player_id) DO UPDATE SET
                            player_name=excluded.player_name,
                            primary_team_id=excluded.primary_team_id,
                            position='P',
                            roster_status='probable',
                            verified_on=excluded.verified_on,
                            source_key=excluded.source_key,
                            source_url=excluded.source_url
                        """,
                        (
                            pitcher.player_id, pitcher.player_name, pitcher.team_id,
                            batch.schedule_date, "https://www.mlb.com/probable-pitchers",
                        ),
                    )
                for team_id, lineup in (
                    (item.home_team_id, item.home_lineup),
                    (item.away_team_id, item.away_lineup),
                ):
                    for player in lineup:
                        self.db.execute(
                            """
                            INSERT INTO players (
                                player_id, sport, player_name, primary_team_id, position,
                                roster_status, verified_on, source_key, source_url
                            ) VALUES (?, 'mlb', ?, ?, ?, 'active', ?, 'mlb_lineups', ?)
                            ON CONFLICT(player_id) DO UPDATE SET
                                player_name=excluded.player_name,
                                primary_team_id=excluded.primary_team_id,
                                position=COALESCE(excluded.position, players.position),
                                roster_status='active',
                                verified_on=excluded.verified_on,
                                source_key=excluded.source_key,
                                source_url=excluded.source_url
                            """,
                            (
                                player.player_id, player.player_name, team_id, player.position,
                                batch.schedule_date, "https://www.mlb.com/starting-lineups",
                            ),
                        )

                home_pid = item.probable_home_pitcher.player_id if item.probable_home_pitcher else None
                away_pid = item.probable_away_pitcher.player_id if item.probable_away_pitcher else None
                home_confirmed = len(item.home_lineup) >= 9
                away_confirmed = len(item.away_lineup) >= 9
                home_order = [player.player_id for player in item.home_lineup] if home_confirmed else []
                away_order = [player.player_id for player in item.away_lineup] if away_confirmed else []
                notes = ["Official MLB schedule/probable-pitcher collection"]
                if home_confirmed and away_confirmed:
                    notes.append("Official structured MLB batting orders collected")
                else:
                    notes.append("One or both structured batting orders remain unconfirmed")
                self.db.execute(
                    """
                    INSERT INTO mlb_game_context (
                        game_id, probable_home_pitcher_id, probable_away_pitcher_id,
                        confirmed_home_pitcher_id, confirmed_away_pitcher_id,
                        home_lineup_status, away_lineup_status,
                        home_batting_order_json, away_batting_order_json,
                        collected_at, source_notes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        probable_home_pitcher_id=excluded.probable_home_pitcher_id,
                        probable_away_pitcher_id=excluded.probable_away_pitcher_id,
                        confirmed_home_pitcher_id=CASE
                            WHEN excluded.home_lineup_status='confirmed' THEN excluded.confirmed_home_pitcher_id
                            ELSE mlb_game_context.confirmed_home_pitcher_id
                        END,
                        confirmed_away_pitcher_id=CASE
                            WHEN excluded.away_lineup_status='confirmed' THEN excluded.confirmed_away_pitcher_id
                            ELSE mlb_game_context.confirmed_away_pitcher_id
                        END,
                        home_lineup_status=excluded.home_lineup_status,
                        away_lineup_status=excluded.away_lineup_status,
                        home_batting_order_json=excluded.home_batting_order_json,
                        away_batting_order_json=excluded.away_batting_order_json,
                        collected_at=excluded.collected_at,
                        source_notes_json=excluded.source_notes_json
                    """,
                    (
                        game_id, home_pid, away_pid,
                        home_pid if home_confirmed and home_pid else None,
                        away_pid if away_confirmed and away_pid else None,
                        'confirmed' if home_confirmed else 'unconfirmed',
                        'confirmed' if away_confirmed else 'unconfirmed',
                        canonical_json(home_order), canonical_json(away_order), now,
                        canonical_json(notes),
                    ),
                )
                stored += 1
        return stored


    def persist_mlb_recent_results(self, batch: MlbRecentResultsBatch) -> int:
        """Preserve official completed MLB results for prediction-time research."""
        ingest_id = self.store_raw(
            source_key="mlb_schedule",
            endpoint_name=f"recent_results_{batch.start_date}_{batch.end_date}",
            payload=batch.raw_payload,
            collected_at=batch.collected_at,
        )
        stored = 0
        with self.db:
            for item in batch.games:
                existing = self.db.execute(
                    "SELECT * FROM historical_mlb_games WHERE source_game_id=?",
                    (item.source_game_id,),
                ).fetchone()
                if existing is not None:
                    identity = (
                        int(existing["season"]),
                        str(existing["home_team_id"]),
                        str(existing["away_team_id"]),
                        int(existing["home_score"]),
                        int(existing["away_score"]),
                    )
                    incoming = (
                        item.season, item.home_team_id, item.away_team_id,
                        item.home_score, item.away_score,
                    )
                    if identity != incoming:
                        raise ValueError(
                            f"Official MLB final result changed identity/score for {item.source_game_id}"
                        )
                    continue
                self.db.execute(
                    """
                    INSERT INTO historical_mlb_games (
                        source_game_id, season, kickoff_time, home_team_id, away_team_id,
                        home_score, away_score, game_number, source_key, ingest_id,
                        source_updated_at, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'mlb_schedule', ?, ?, ?)
                    """,
                    (
                        item.source_game_id, item.season, iso(item.game_time),
                        item.home_team_id, item.away_team_id, item.home_score,
                        item.away_score, ingest_id, iso(batch.collected_at),
                        iso(batch.collected_at),
                    ),
                )
                stored += 1
        return stored


    def persist_mlb_transactions(self, batch: MlbTransactionsBatch) -> dict[str, int]:
        """Persist official MLB transactions plus conservative roster/IL evidence.

        The raw official response is preserved.  Date-only transaction records use
        their first collection time as the prediction-time availability boundary,
        preventing an event from being treated as known earlier than we observed it.
        """
        ingest_id = self.store_raw(
            source_key="mlb_transactions",
            endpoint_name=f"transactions_{batch.start_date}_{batch.end_date}",
            payload=batch.raw_payload,
            collected_at=batch.collected_at,
        )
        counts = {"transactions": 0, "rosters": 0, "injuries": 0}
        source_url = "https://www.mlb.com/transactions"
        with self.db:
            for item in batch.records:
                if item.source_updated_at > batch.collected_at:
                    raise ValueError("MLB transaction source time cannot be after collection time")

                existing = self.db.execute(
                    """
                    SELECT source_updated_at FROM transaction_snapshots
                    WHERE player_id = ?
                      AND COALESCE(from_team_id, '') = COALESCE(?, '')
                      AND COALESCE(to_team_id, '') = COALESCE(?, '')
                      AND transaction_type = ?
                      AND transaction_date = ?
                      AND description = ?
                      AND source_key = 'mlb_transactions'
                    ORDER BY transaction_snapshot_id ASC
                    LIMIT 1
                    """,
                    (
                        item.player_id,
                        item.from_team_id,
                        item.to_team_id,
                        item.transaction_type,
                        item.transaction_date,
                        item.description,
                    ),
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        """
                        INSERT INTO transaction_snapshots (
                            player_id, player_name, from_team_id, to_team_id, position,
                            transaction_type, transaction_date, description, official,
                            source_key, source_updated_at, collected_at, ingest_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'mlb_transactions', ?, ?, ?)
                        """,
                        (
                            item.player_id,
                            item.player_name,
                            item.from_team_id,
                            item.to_team_id,
                            item.position,
                            item.transaction_type,
                            item.transaction_date,
                            item.description,
                            iso(item.source_updated_at),
                            iso(batch.collected_at),
                            ingest_id,
                        ),
                    )
                    effective_source_time = item.source_updated_at
                    counts["transactions"] += 1
                else:
                    effective_source_time = datetime.fromisoformat(str(existing["source_updated_at"]))

                team_id = item.to_team_id or item.from_team_id
                self.db.execute(
                    """
                    INSERT INTO players (
                        player_id, sport, player_name, primary_team_id, position,
                        roster_status, verified_on, source_key, source_url
                    ) VALUES (?, 'mlb', ?, ?, ?, ?, ?, 'mlb_transactions', ?)
                    ON CONFLICT(player_id) DO UPDATE SET
                        player_name=excluded.player_name,
                        primary_team_id=COALESCE(excluded.primary_team_id, players.primary_team_id),
                        position=COALESCE(excluded.position, players.position)
                    """,
                    (
                        item.player_id,
                        item.player_name,
                        team_id,
                        item.position,
                        item.roster_status,
                        item.transaction_date,
                        source_url,
                    ),
                )

                if team_id and item.roster_status:
                    roster_exists = self.db.execute(
                        """
                        SELECT 1 FROM roster_snapshots
                        WHERE player_id = ? AND team_id = ? AND roster_status = ?
                          AND source_key = 'mlb_transactions'
                          AND source_updated_at = ?
                        """,
                        (
                            item.player_id,
                            team_id,
                            item.roster_status,
                            iso(effective_source_time),
                        ),
                    ).fetchone()
                    if roster_exists is None:
                        self.db.execute(
                            """
                            INSERT INTO roster_snapshots (
                                player_id, player_name, team_id, position, roster_status,
                                starter_status, depth_role, source_key, source_updated_at,
                                collected_at, ingest_id
                            ) VALUES (?, ?, ?, ?, ?, NULL, 'official_transaction',
                                      'mlb_transactions', ?, ?, ?)
                            """,
                            (
                                item.player_id,
                                item.player_name,
                                team_id,
                                item.position,
                                item.roster_status,
                                iso(effective_source_time),
                                iso(batch.collected_at),
                                ingest_id,
                            ),
                        )
                        counts["rosters"] += 1

                if team_id and item.injured_list:
                    injury_exists = self.db.execute(
                        """
                        SELECT 1 FROM injury_snapshots
                        WHERE player_id = ? AND team_id = ?
                          AND source_key = 'mlb_transactions'
                          AND source_updated_at = ?
                          AND game_status = 'out'
                        """,
                        (item.player_id, team_id, iso(effective_source_time)),
                    ).fetchone()
                    if injury_exists is None:
                        self.db.execute(
                            """
                            INSERT INTO injury_snapshots (
                                game_id, player_id, player_name, team_id, position,
                                injury, practice_status, game_status, official,
                                source_key, source_updated_at, collected_at, ingest_id
                            ) VALUES (NULL, ?, ?, ?, ?, 'injured_list', NULL, 'out', 1,
                                      'mlb_transactions', ?, ?, ?)
                            """,
                            (
                                item.player_id,
                                item.player_name,
                                team_id,
                                item.position,
                                iso(effective_source_time),
                                iso(batch.collected_at),
                                ingest_id,
                            ),
                        )
                        counts["injuries"] += 1
        return counts

    def persist_rosters(self, batch: NflverseBatch) -> int:
        ingest_id = self.store_raw(
            source_key="nflverse",
            endpoint_name=f"rosters_{batch.season}",
            payload=batch.raw_text,
            collected_at=batch.collected_at,
            source_updated_at=batch.source_updated_at,
        )
        stored = 0
        with self.db:
            for item in batch.records:
                if not isinstance(item, RosterRecord):
                    raise TypeError("Roster batch contains a non-roster record")
                exists = self.db.execute(
                    """
                    SELECT 1 FROM roster_snapshots
                    WHERE player_id = ? AND team_id = ? AND roster_status = ?
                      AND source_updated_at = ? AND source_key = 'nflverse'
                    """,
                    (
                        item.player_id,
                        item.team_id,
                        item.roster_status,
                        iso(item.source_updated_at),
                    ),
                ).fetchone()
                if exists:
                    continue
                self.db.execute(
                    """
                    INSERT INTO roster_snapshots (
                        player_id, player_name, team_id, position, roster_status,
                        starter_status, depth_role, source_key, source_updated_at,
                        collected_at, ingest_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'nflverse', ?, ?, ?)
                    """,
                    (
                        item.player_id,
                        item.player_name,
                        item.team_id,
                        item.position,
                        item.roster_status,
                        item.starter_status,
                        item.depth_role,
                        iso(item.source_updated_at),
                        iso(batch.collected_at),
                        ingest_id,
                    ),
                )
                stored += 1
        return stored

    def persist_injuries(self, batch: NflverseBatch) -> int:
        ingest_id = self.store_raw(
            source_key="nflverse",
            endpoint_name=f"injuries_{batch.season}",
            payload=batch.raw_text,
            collected_at=batch.collected_at,
            source_updated_at=batch.source_updated_at,
        )
        stored = 0
        with self.db:
            for item in batch.records:
                if not isinstance(item, InjuryRecord):
                    raise TypeError("Injury batch contains a non-injury record")
                game = self.db.execute(
                    """
                    SELECT game_id FROM games
                    WHERE season = ? AND season_type = ? AND week = ?
                      AND (home_team_id = ? OR away_team_id = ?)
                    """,
                    (item.season, item.season_type, item.week, item.team_id, item.team_id),
                ).fetchall()
                game_id = str(game[0][0]) if len(game) == 1 else None
                exists = self.db.execute(
                    """
                    SELECT 1 FROM injury_snapshots
                    WHERE player_id = ? AND team_id = ? AND source_updated_at = ?
                      AND COALESCE(practice_status, '') = COALESCE(?, '')
                      AND COALESCE(game_status, '') = COALESCE(?, '')
                      AND source_key = 'nflverse'
                    """,
                    (
                        item.player_id,
                        item.team_id,
                        iso(item.source_updated_at),
                        item.practice_status,
                        item.game_status,
                    ),
                ).fetchone()
                if exists:
                    continue
                self.db.execute(
                    """
                    INSERT INTO injury_snapshots (
                        game_id, player_id, player_name, team_id, position,
                        injury, practice_status, game_status, official,
                        source_key, source_updated_at, collected_at, ingest_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'nflverse', ?, ?, ?)
                    """,
                    (
                        game_id,
                        item.player_id,
                        item.player_name,
                        item.team_id,
                        item.position,
                        item.injury,
                        item.practice_status,
                        item.game_status,
                        iso(item.source_updated_at),
                        iso(batch.collected_at),
                        ingest_id,
                    ),
                )
                stored += 1
        return stored

    def persist_weather(self, game_id: str, batch: WeatherBatch, roof_status: str) -> int:
        ingest_id = self.store_raw(
            source_key="nws",
            endpoint_name=f"forecast_{game_id}",
            payload=batch.raw_payload,
            collected_at=batch.record.collected_at,
            source_updated_at=batch.record.source_updated_at,
        )
        record = batch.record
        exists = self.db.execute(
            """
            SELECT weather_snapshot_id FROM weather_snapshots
            WHERE game_id = ? AND source_updated_at = ? AND forecast_for = ?
            """,
            (game_id, iso(record.source_updated_at), iso(record.forecast_for)),
        ).fetchone()
        if exists:
            return 0
        self.db.execute(
            """
            INSERT INTO weather_snapshots (
                game_id, forecast_for, temperature_f, sustained_wind_mph,
                wind_gust_mph, precipitation_probability, precipitation_type,
                alerts_json, roof_status, source_key, source_updated_at,
                collected_at, ingest_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'nws', ?, ?, ?)
            """,
            (
                game_id,
                iso(record.forecast_for),
                record.temperature_f,
                record.sustained_wind_mph,
                record.wind_gust_mph,
                record.precipitation_probability,
                record.precipitation_type,
                canonical_json(record.alerts),
                roof_status,
                iso(record.source_updated_at),
                iso(record.collected_at),
                ingest_id,
            ),
        )
        self.db.commit()
        return 1

    def persist_official_check(
        self, check: OfficialPageCheck, *, game_ids: Iterable[str] = ()
    ) -> int:
        self.store_raw(
            source_key=f"nfl_{check.check_type}",
            endpoint_name=f"official_{check.check_type}_page_check",
            payload={
                "source_url": check.source_url,
                "content_sha256": check.content_sha256,
                "access_status": check.access_status,
                "warning": check.warning,
            },
            collected_at=check.checked_at,
            http_status=check.http_status,
        )
        stored = 0
        with self.db:
            for game_id in game_ids:
                self.db.execute(
                    """
                    INSERT INTO source_checks (
                        game_id, source_key, check_type, access_status,
                        checked_at, missing_information, reviewer_notes, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        f"nfl_{check.check_type}",
                        check.check_type,
                        check.access_status,
                        iso(check.checked_at),
                        check.warning or "Page reachability only; player-level facts still require official confirmation.",
                        "This check does not count as a confirmed player injury or transaction record.",
                        check.source_url,
                    ),
                )
                stored += 1
        return stored


    def persist_mlb_official_check(
        self, check: OfficialMlbPageCheck, *, game_ids: Iterable[str] = ()
    ) -> int:
        """Persist an official MLB page reachability check without inventing player facts."""
        self.store_raw(
            source_key=check.source_key,
            endpoint_name=f"official_mlb_{check.check_type}_page_check",
            payload={
                "source_url": check.source_url,
                "content_sha256": check.content_sha256,
                "access_status": check.access_status,
                "warning": check.warning,
            },
            collected_at=check.checked_at,
            http_status=check.http_status,
        )
        stored = 0
        with self.db:
            for game_id in game_ids:
                self.db.execute(
                    """
                    INSERT INTO source_checks (
                        game_id, source_key, check_type, access_status,
                        checked_at, missing_information, reviewer_notes, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        check.source_key,
                        check.check_type,
                        check.access_status,
                        iso(check.checked_at),
                        check.warning
                        or "Page reachability only; structured player-level confirmation is still required.",
                        "This page check proves source reachability/identity only and cannot confirm a lineup, injury, transaction, or starter by itself.",
                        check.source_url,
                    ),
                )
                stored += 1
        return stored

    def persist_archive(self, batch: ArchiveBatch, directory: str | Path) -> Path:
        base = Path(directory).resolve()
        base.mkdir(parents=True, exist_ok=True)
        destination = base / f"play_by_play_{batch.season}.csv.gz"
        destination.write_bytes(batch.content)
        checksum = hashlib.sha256(batch.content).hexdigest()
        self.db.execute(
            """
            INSERT OR IGNORE INTO data_files (
                source_key, dataset_name, season, source_url, local_path,
                content_sha256, size_bytes, source_updated_at, collected_at
            ) VALUES ('nflverse', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch.dataset_name,
                batch.season,
                batch.source_url,
                str(destination),
                checksum,
                len(batch.content),
                iso(batch.source_updated_at) if batch.source_updated_at else None,
                iso(batch.collected_at),
            ),
        )
        self.db.commit()
        return destination
