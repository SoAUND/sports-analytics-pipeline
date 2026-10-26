"""SQLite creation and reference-data helpers."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
import sqlite3


def connect(database: str | Path = ":memory:") -> sqlite3.Connection:
    path = str(database)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    schema = files("sports_analytics_pipeline.data").joinpath("schema.sql").read_text(encoding="utf-8")
    connection.executescript(schema)
    _migrate_model_runs(connection)
    _migrate_historical_training_games(connection)
    _migrate_multisport_phase2(connection)
    _migrate_multisport_history_collectors(connection)
    connection.commit()


def _migrate_model_runs(connection: sqlite3.Connection) -> None:
    """Add Phase 3 evidence fields to an existing Phase 1/2 database."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(model_runs)")
    }
    additions = {
        "market_spread_points": "REAL",
        "oos_decision_count": "INTEGER NOT NULL DEFAULT 0",
        "calibration_status": "TEXT NOT NULL DEFAULT 'untested'",
        "clv_status": "TEXT NOT NULL DEFAULT 'unknown'",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE model_runs ADD COLUMN {name} {definition}"
            )


def _migrate_historical_training_games(connection: sqlite3.Connection) -> None:
    """Add Phase 4.1 closing-price and pregame context to an existing database."""
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(historical_training_games)")
    }
    additions = {
        "home_spread_decimal_odds": "REAL",
        "away_spread_decimal_odds": "REAL",
        "home_market_no_vig_probability": "REAL",
        "home_rest_days": "INTEGER",
        "away_rest_days": "INTEGER",
        "home_qb_id": "TEXT",
        "away_qb_id": "TEXT",
        "home_coach": "TEXT",
        "away_coach": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE historical_training_games ADD COLUMN {name} {definition}"
            )


def _migrate_multisport_phase2(connection: sqlite3.Connection) -> None:
    """Create additive, test-only tables for sports outside the locked NFL/MLB schema.

    We intentionally do not widen the original NFL/MLB CHECK constraints in place.
    Existing production and paper records keep their original guarantees while the
    new sports mature in their own auditable tables.
    """
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS multisport_participants (
            participant_id TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_key TEXT NOT NULL,
            source_participant_id TEXT NOT NULL,
            full_name TEXT NOT NULL,
            participant_type TEXT NOT NULL CHECK (participant_type IN ('team','individual')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            active_status TEXT NOT NULL DEFAULT 'provider_listed'
                CHECK (active_status IN ('provider_listed','inactive','unknown')),
            UNIQUE (provider_sport_key, source_participant_id),
            CHECK (last_seen_at >= first_seen_at)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_participant_sport_name
        ON multisport_participants(sport, full_name);

        CREATE TABLE IF NOT EXISTS multisport_events (
            event_id TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_key TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            commence_time TEXT NOT NULL,
            home_participant_id TEXT NOT NULL REFERENCES multisport_participants(participant_id),
            away_participant_id TEXT NOT NULL REFERENCES multisport_participants(participant_id),
            venue_id TEXT,
            venue_name TEXT,
            source_venue_id TEXT,
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN ('scheduled','in_play','postponed','cancelled','final')),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            UNIQUE (provider_sport_key, source_event_id),
            CHECK (home_participant_id <> away_participant_id),
            CHECK (last_seen_at >= first_seen_at)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_events_sport_time
        ON multisport_events(sport, commence_time);

        CREATE TABLE IF NOT EXISTS multisport_raw_ingests (
            ingest_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_key TEXT NOT NULL,
            endpoint_name TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            UNIQUE (source_key, provider_sport_key, endpoint_name, content_sha256)
        );

        CREATE TABLE IF NOT EXISTS multisport_odds_snapshots (
            odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            ingest_id INTEGER REFERENCES multisport_raw_ingests(ingest_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_key TEXT NOT NULL,
            sportsbook_key TEXT NOT NULL,
            sportsbook_title TEXT NOT NULL,
            market_key TEXT NOT NULL CHECK (
                market_key IN ('spreads','three_way_moneyline','moneylines','match_moneyline')
            ),
            provider_market_key TEXT NOT NULL CHECK (provider_market_key IN ('spreads','h2h')),
            period TEXT NOT NULL CHECK (period IN ('full_game','full_fight','full_match')),
            settlement_rule TEXT NOT NULL,
            outcome_participant_id TEXT REFERENCES multisport_participants(participant_id),
            outcome_name TEXT NOT NULL,
            outcome_role TEXT NOT NULL CHECK (outcome_role IN ('home','away','draw')),
            points REAL,
            decimal_odds REAL NOT NULL CHECK (decimal_odds > 1 AND decimal_odds <= 100),
            source_updated_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            market_status TEXT NOT NULL CHECK (market_status IN ('open','suspended','stale','unavailable')),
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            CHECK (
                (outcome_role = 'draw' AND outcome_participant_id IS NULL)
                OR (outcome_role IN ('home','away') AND outcome_participant_id IS NOT NULL)
            ),
            CHECK (
                (provider_market_key = 'spreads' AND points IS NOT NULL)
                OR (provider_market_key = 'h2h' AND points IS NULL)
            ),
            UNIQUE (
                event_id, sportsbook_key, market_key, period, outcome_role,
                points, source_updated_at
            )
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_odds_event_time
        ON multisport_odds_snapshots(event_id, collected_at, sportsbook_key);

        CREATE TABLE IF NOT EXISTS multisport_research_checks (
            research_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            category TEXT NOT NULL CHECK (
                category IN (
                    'official_status','injury_availability','lineup_or_participants',
                    'statistics_analytics','transactions_or_changes','current_odds',
                    'weather_environment','rest_travel','weigh_in_status'
                )
            ),
            source_key TEXT NOT NULL,
            source_url TEXT,
            official INTEGER NOT NULL CHECK (official IN (0,1)),
            verification_status TEXT NOT NULL CHECK (
                verification_status IN ('verified','conditional','unavailable','conflict')
            ),
            source_updated_at TEXT,
            collected_at TEXT NOT NULL,
            notes TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            evidence_hash TEXT,
            UNIQUE (event_id, category, source_key, collected_at)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_research_event
        ON multisport_research_checks(event_id, category, collected_at);

        CREATE TABLE IF NOT EXISTS multisport_validation_state (
            validation_bucket TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            target_decisions INTEGER NOT NULL DEFAULT 100 CHECK (target_decisions >= 100),
            completed_decisions INTEGER NOT NULL DEFAULT 0 CHECK (completed_decisions >= 0),
            approved INTEGER NOT NULL DEFAULT 0 CHECK (approved = 0),
            status TEXT NOT NULL DEFAULT 'test_only' CHECK (status = 'test_only'),
            updated_at TEXT NOT NULL
        );
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(multisport_events)")
    }
    for name in ("venue_id", "venue_name", "source_venue_id"):
        if name not in columns:
            connection.execute(f"ALTER TABLE multisport_events ADD COLUMN {name} TEXT")


def _migrate_multisport_history_collectors(connection: sqlite3.Connection) -> None:
    """Create the test-only audit ledger for automatic historical collection."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS multisport_history_runs (
            history_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_key TEXT NOT NULL,
            requested_sports_json TEXT NOT NULL,
            range_start TEXT NOT NULL,
            range_end TEXT NOT NULL,
            bookmakers_json TEXT NOT NULL,
            include_odds INTEGER NOT NULL CHECK (include_odds IN (0,1)),
            max_requests INTEGER NOT NULL CHECK (max_requests >= 1),
            requests_made INTEGER NOT NULL DEFAULT 0 CHECK (requests_made >= 0),
            events_seen INTEGER NOT NULL DEFAULT 0 CHECK (events_seen >= 0),
            events_upserted INTEGER NOT NULL DEFAULT 0 CHECK (events_upserted >= 0),
            odds_events_collected INTEGER NOT NULL DEFAULT 0
                CHECK (odds_events_collected >= 0),
            status TEXT NOT NULL CHECK (status IN ('running','complete','partial','failed')),
            error_summary TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            CHECK (range_end > range_start)
        );

        CREATE TABLE IF NOT EXISTS multisport_historical_events (
            historical_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_key TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_slug TEXT NOT NULL,
            provider_league_slug TEXT NOT NULL,
            provider_league_name TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            event_time TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status = 'settled'),
            home_source_id TEXT,
            away_source_id TEXT,
            home_name TEXT NOT NULL,
            away_name TEXT NOT NULL,
            home_score REAL,
            away_score REAL,
            result_role TEXT NOT NULL CHECK (result_role IN ('home','away','draw','unknown')),
            scores_json TEXT NOT NULL,
            result_payload_json TEXT NOT NULL,
            odds_market_name TEXT,
            odds_payload_json TEXT,
            odds_collected_at TEXT,
            first_collected_at TEXT NOT NULL,
            last_collected_at TEXT NOT NULL,
            training_eligible INTEGER NOT NULL DEFAULT 0 CHECK (training_eligible = 0),
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            UNIQUE (provider_key, source_event_id),
            CHECK (home_name <> away_name),
            CHECK (last_collected_at >= first_collected_at)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_history_sport_time
        ON multisport_historical_events(sport, event_time);

        CREATE TABLE IF NOT EXISTS multisport_historical_feature_evidence (
            historical_feature_evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_key TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            feature_name TEXT NOT NULL,
            feature_value REAL NOT NULL,
            feature_version TEXT NOT NULL,
            source_key TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_updated_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            UNIQUE (
                provider_key, source_event_id, sport, feature_name,
                feature_version, source_key, source_updated_at
            ),
            FOREIGN KEY (provider_key, source_event_id)
                REFERENCES multisport_historical_events(provider_key, source_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_history_evidence_event
        ON multisport_historical_feature_evidence(
            provider_key, source_event_id, sport, feature_version, source_updated_at
        );

        CREATE TABLE IF NOT EXISTS multisport_historical_event_verifications (
            historical_event_verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_key TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            verifier_key TEXT NOT NULL,
            verifier_event_id TEXT,
            verification_status TEXT NOT NULL
                CHECK (verification_status IN ('verified','unmatched')),
            verified_at TEXT NOT NULL,
            evidence_json TEXT NOT NULL DEFAULT '{}',
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            UNIQUE (provider_key, source_event_id, sport, verifier_key),
            FOREIGN KEY (provider_key, source_event_id)
                REFERENCES multisport_historical_events(provider_key, source_event_id)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_history_verification_status
        ON multisport_historical_event_verifications(
            sport, verifier_key, verification_status, verified_at
        );

        CREATE TABLE IF NOT EXISTS multisport_history_example_audits (
            history_example_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_key TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            feature_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('blocked','created','existing')),
            source_cutoff TEXT,
            available_features_json TEXT NOT NULL,
            missing_features_json TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            first_attempted_at TEXT NOT NULL,
            last_attempted_at TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
            test_only INTEGER NOT NULL DEFAULT 1 CHECK (test_only = 1),
            UNIQUE (provider_key, source_event_id, sport, feature_version),
            FOREIGN KEY (provider_key, source_event_id)
                REFERENCES multisport_historical_events(provider_key, source_event_id),
            CHECK (last_attempted_at >= first_attempted_at)
        );
        """
    )


def required_table_names() -> set[str]:
    return {
        "games",
        "teams",
        "odds_snapshots",
        "injury_snapshots",
        "roster_snapshots",
        "weather_snapshots",
        "team_game_features",
        "model_runs",
        "historical_training_games",
        "model_artifacts",
        "evaluation_contexts",
        "decisions",
        "results",
        "paper_decisions",
        "paper_results",
        "source_checks",
        "audit_log",
    }


def collection_table_names() -> set[str]:
    """Supporting Phase 2 tables that preserve collection history."""
    return {
        "collection_runs",
        "game_source_ids",
        "transaction_snapshots",
        "data_files",
        "raw_ingests",
    }


def multisport_table_names() -> set[str]:
    """Additive Phase 2 tables for the new test-only sports."""
    return {
        "multisport_participants",
        "multisport_events",
        "multisport_raw_ingests",
        "multisport_odds_snapshots",
        "multisport_research_checks",
        "multisport_validation_state",
        "multisport_history_runs",
        "multisport_historical_events",
        "multisport_historical_feature_evidence",
        "multisport_historical_event_verifications",
        "multisport_history_example_audits",
    }
