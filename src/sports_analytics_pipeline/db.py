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
    _migrate_multisport_teams(connection)
    _migrate_multisport_aliases(connection)
    _migrate_games_sport(connection)
    _migrate_moneyline_odds(connection)
    _migrate_model_runs(connection)
    _migrate_paper_decisions_moneylines(connection)
    _migrate_historical_training_games(connection)
    connection.commit()



def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (table,)
    ).fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _migrate_multisport_teams(connection: sqlite3.Connection) -> None:
    """Make team identity sport-aware while preserving every existing NFL row."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(teams)")}
    sql = _table_sql(connection, "teams")
    if "sport" in columns and "AL" in sql and "Central" in sql:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        before = connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        connection.execute("DROP TABLE IF EXISTS teams_multisport_new")
        connection.execute(
            """
            CREATE TABLE teams_multisport_new (
                team_id TEXT PRIMARY KEY,
                sport TEXT NOT NULL DEFAULT 'nfl' CHECK (sport IN ('nfl', 'mlb')),
                official_name TEXT NOT NULL,
                city_name TEXT NOT NULL,
                nickname TEXT NOT NULL,
                abbreviation TEXT NOT NULL,
                conference TEXT NOT NULL CHECK (conference IN ('AFC', 'NFC', 'AL', 'NL')),
                division TEXT NOT NULL CHECK (division IN ('East', 'North', 'South', 'West', 'Central')),
                primary_stadium_id TEXT NOT NULL REFERENCES stadiums(stadium_id),
                active_from_season INTEGER NOT NULL,
                active_through_season INTEGER,
                verified_on TEXT NOT NULL,
                CHECK (active_through_season IS NULL OR active_through_season >= active_from_season),
                UNIQUE (sport, official_name),
                UNIQUE (sport, abbreviation)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO teams_multisport_new (
                team_id, sport, official_name, city_name, nickname, abbreviation,
                conference, division, primary_stadium_id, active_from_season,
                active_through_season, verified_on
            )
            SELECT team_id, 'nfl', official_name, city_name, nickname, abbreviation,
                   conference, division, primary_stadium_id, active_from_season,
                   active_through_season, verified_on
            FROM teams
            """
        )
        after = connection.execute("SELECT COUNT(*) FROM teams_multisport_new").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError("Team migration row-count verification failed")
        connection.execute("DROP TABLE teams")
        connection.execute("ALTER TABLE teams_multisport_new RENAME TO teams")
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_multisport_aliases(connection: sqlite3.Connection) -> None:
    """Allow the same human alias in different sports without ambiguous global mapping."""
    columns = {row[1] for row in connection.execute("PRAGMA table_info(team_aliases)")}
    sql = _table_sql(connection, "team_aliases")
    if "sport" in columns and "PRIMARY KEY (sport, alias)" in sql:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        before = connection.execute("SELECT COUNT(*) FROM team_aliases").fetchone()[0]
        connection.execute("DROP TABLE IF EXISTS team_aliases_multisport_new")
        connection.execute(
            """
            CREATE TABLE team_aliases_multisport_new (
                alias TEXT NOT NULL COLLATE NOCASE,
                sport TEXT NOT NULL DEFAULT 'nfl' CHECK (sport IN ('nfl', 'mlb')),
                team_id TEXT NOT NULL REFERENCES teams(team_id),
                source_key TEXT NOT NULL DEFAULT 'common',
                PRIMARY KEY (sport, alias)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO team_aliases_multisport_new(alias, sport, team_id, source_key)
            SELECT alias, 'nfl', team_id, source_key FROM team_aliases
            """
        )
        after = connection.execute("SELECT COUNT(*) FROM team_aliases_multisport_new").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError("Alias migration row-count verification failed")
        connection.execute("DROP TABLE team_aliases")
        connection.execute("ALTER TABLE team_aliases_multisport_new RENAME TO team_aliases")
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_games_sport(connection: sqlite3.Connection) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(games)")}
    if "sport" not in columns:
        connection.execute("ALTER TABLE games ADD COLUMN sport TEXT NOT NULL DEFAULT 'nfl'")


def _migrate_moneyline_odds(connection: sqlite3.Connection) -> None:
    """Expand the shared odds ledger to full-game spreads and MLB moneylines."""
    sql = _table_sql(connection, "odds_snapshots")
    if "moneylines" in sql and "spread_points IS NULL" in sql:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("DROP TRIGGER IF EXISTS prevent_future_odds_decision")
        before = connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0]
        connection.execute("DROP TABLE IF EXISTS odds_snapshots_multimarket_new")
        connection.execute(
            """
            CREATE TABLE odds_snapshots_multimarket_new (
                odds_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL REFERENCES games(game_id),
                ingest_id INTEGER REFERENCES raw_ingests(ingest_id),
                sportsbook_key TEXT NOT NULL,
                market_key TEXT NOT NULL CHECK (market_key IN ('spreads', 'moneylines')),
                period TEXT NOT NULL CHECK (period = 'full_game'),
                settlement_rule TEXT NOT NULL,
                outcome_team_id TEXT NOT NULL REFERENCES teams(team_id),
                opponent_team_id TEXT NOT NULL REFERENCES teams(team_id),
                spread_points REAL CHECK ((market_key = 'spreads' AND spread_points BETWEEN -40 AND 40) OR (market_key = 'moneylines' AND spread_points IS NULL)),
                decimal_odds REAL NOT NULL CHECK (decimal_odds > 1 AND decimal_odds <= 100),
                american_odds REAL,
                source_updated_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                market_status TEXT NOT NULL CHECK (market_status IN ('open', 'suspended', 'stale', 'unavailable')),
                is_accessible INTEGER NOT NULL DEFAULT 0 CHECK (is_accessible IN (0, 1)),
                is_consensus_eligible INTEGER NOT NULL DEFAULT 0 CHECK (is_consensus_eligible IN (0, 1)),
                CHECK (outcome_team_id <> opponent_team_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO odds_snapshots_multimarket_new (
                odds_snapshot_id, game_id, ingest_id, sportsbook_key, market_key,
                period, settlement_rule, outcome_team_id, opponent_team_id,
                spread_points, decimal_odds, american_odds, source_updated_at,
                collected_at, market_status, is_accessible, is_consensus_eligible
            )
            SELECT odds_snapshot_id, game_id, ingest_id, sportsbook_key, market_key,
                   period, settlement_rule, outcome_team_id, opponent_team_id,
                   spread_points, decimal_odds, american_odds, source_updated_at,
                   collected_at, market_status, is_accessible, is_consensus_eligible
            FROM odds_snapshots
            """
        )
        after = connection.execute("SELECT COUNT(*) FROM odds_snapshots_multimarket_new").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError("Odds migration row-count verification failed")
        connection.execute("DROP TABLE odds_snapshots")
        connection.execute("ALTER TABLE odds_snapshots_multimarket_new RENAME TO odds_snapshots")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_odds_game_time ON odds_snapshots(game_id, collected_at, sportsbook_key)")
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_odds_exact_offer
               ON odds_snapshots(
                 game_id, sportsbook_key, market_key, period, settlement_rule,
                 outcome_team_id, COALESCE(spread_points, 999.0), source_updated_at
               )"""
        )
        connection.execute(
            """
            CREATE TRIGGER prevent_future_odds_decision
            BEFORE INSERT ON decisions
            WHEN NEW.selected_odds_snapshot_id IS NOT NULL
            BEGIN
                SELECT CASE WHEN EXISTS (
                    SELECT 1
                    FROM odds_snapshots o
                    JOIN model_runs m ON m.model_run_id = NEW.model_run_id
                    WHERE o.odds_snapshot_id = NEW.selected_odds_snapshot_id
                      AND (o.source_updated_at > m.prediction_time OR o.collected_at > m.prediction_time)
                ) THEN RAISE(ABORT, 'selected odds did not exist by prediction time') END;
            END
            """
        )
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_paper_decisions_moneylines(connection: sqlite3.Connection) -> None:
    sql = _table_sql(connection, "paper_decisions")
    if "moneylines" in sql:
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        before = connection.execute("SELECT COUNT(*) FROM paper_decisions").fetchone()[0]
        connection.execute("DROP TABLE IF EXISTS paper_decisions_multimarket_new")
        connection.execute(
            """
            CREATE TABLE paper_decisions_multimarket_new (
                paper_decision_id TEXT PRIMARY KEY,
                game_id TEXT NOT NULL REFERENCES games(game_id),
                source_decision_id TEXT REFERENCES decisions(decision_id),
                model_run_id TEXT REFERENCES model_runs(model_run_id),
                season_type TEXT NOT NULL CHECK (season_type IN ('preseason', 'regular', 'postseason')),
                market_key TEXT NOT NULL DEFAULT 'spreads' CHECK (market_key IN ('spreads', 'moneylines')),
                period TEXT NOT NULL DEFAULT 'full_game' CHECK (period = 'full_game'),
                decision_type TEXT NOT NULL CHECK (decision_type IN ('test_only_selection', 'pass')),
                selected_team_id TEXT REFERENCES teams(team_id),
                spread_points REAL,
                sportsbook_key TEXT,
                selected_odds_snapshot_id INTEGER REFERENCES odds_snapshots(odds_snapshot_id),
                decimal_odds REAL,
                price_time TEXT,
                model_probability REAL,
                conservative_probability REAL,
                push_probability REAL,
                break_even_probability REAL,
                probability_edge REAL,
                ev_per_dollar REAL,
                minimum_acceptable_decimal_odds REAL,
                pick_strength_score INTEGER,
                confidence_label TEXT,
                model_version TEXT,
                feature_version TEXT,
                data_version TEXT,
                rule_version TEXT NOT NULL,
                gate_results_json TEXT NOT NULL DEFAULT '{}',
                reasons_json TEXT NOT NULL DEFAULT '[]',
                loss_reasons_json TEXT NOT NULL DEFAULT '[]',
                source_notes_json TEXT NOT NULL DEFAULT '[]',
                pass_reason TEXT,
                countable_oos INTEGER NOT NULL CHECK (countable_oos IN (0, 1)),
                decision_time TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (game_id, market_key, period),
                CHECK (decision_type = 'pass' OR (selected_team_id IS NOT NULL AND selected_odds_snapshot_id IS NOT NULL))
            )
            """
        )
        cols = [row[1] for row in connection.execute("PRAGMA table_info(paper_decisions)")]
        col_list = ", ".join(cols)
        connection.execute(f"INSERT INTO paper_decisions_multimarket_new ({col_list}) SELECT {col_list} FROM paper_decisions")
        after = connection.execute("SELECT COUNT(*) FROM paper_decisions_multimarket_new").fetchone()[0]
        if after != before:
            raise sqlite3.IntegrityError("Paper-decision migration row-count verification failed")
        connection.execute("DROP TABLE paper_decisions")
        connection.execute("ALTER TABLE paper_decisions_multimarket_new RENAME TO paper_decisions")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_paper_decisions_time ON paper_decisions(countable_oos, decision_time)")
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _migrate_model_runs(connection: sqlite3.Connection) -> None:
    """Add Phase 3 evidence fields to an existing Phase 1/2 database."""
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(model_runs)")
    }
    additions = {
        "sport": "TEXT NOT NULL DEFAULT 'nfl'",
        "market_key": "TEXT NOT NULL DEFAULT 'spreads'",
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
        "players",
        "mlb_game_context",
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
