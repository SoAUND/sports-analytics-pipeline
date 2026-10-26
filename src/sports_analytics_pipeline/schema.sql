PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stadiums (
    stadium_id TEXT PRIMARY KEY,
    official_name TEXT NOT NULL,
    city TEXT NOT NULL,
    state_or_region TEXT NOT NULL,
    country_code TEXT NOT NULL DEFAULT 'US',
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    time_zone TEXT NOT NULL,
    roof_type TEXT NOT NULL CHECK (roof_type IN ('open', 'retractable', 'fixed', 'canopy_open_sides')),
    surface_type TEXT NOT NULL CHECK (surface_type IN ('natural_grass', 'artificial_turf', 'hybrid_grass')),
    weather_exposed INTEGER NOT NULL CHECK (weather_exposed IN (0, 1)),
    valid_from_season INTEGER NOT NULL,
    valid_through_season INTEGER,
    verified_on TEXT NOT NULL,
    source_url TEXT NOT NULL,
    CHECK (valid_through_season IS NULL OR valid_through_season >= valid_from_season)
);

CREATE TABLE IF NOT EXISTS teams (
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
);

CREATE TABLE IF NOT EXISTS team_aliases (
    alias TEXT NOT NULL COLLATE NOCASE,
    sport TEXT NOT NULL DEFAULT 'nfl' CHECK (sport IN ('nfl', 'mlb')),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    source_key TEXT NOT NULL DEFAULT 'common',
    PRIMARY KEY (sport, alias)
);

CREATE TABLE IF NOT EXISTS sources (
    source_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 4),
    base_url TEXT NOT NULL,
    official INTEGER NOT NULL CHECK (official IN (0, 1)),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sportsbooks (
    sportsbook_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    consensus_eligible INTEGER NOT NULL DEFAULT 0 CHECK (consensus_eligible IN (0, 1)),
    accessible INTEGER NOT NULL DEFAULT 0 CHECK (accessible IN (0, 1)),
    approved_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,
    source_game_id TEXT,
    sport TEXT NOT NULL DEFAULT 'nfl' CHECK (sport IN ('nfl', 'mlb')),
    season INTEGER NOT NULL,
    season_type TEXT NOT NULL CHECK (season_type IN ('preseason', 'regular', 'postseason')),
    week INTEGER NOT NULL CHECK (week BETWEEN 0 AND 25),
    home_team_id TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id TEXT NOT NULL REFERENCES teams(team_id),
    kickoff_time TEXT NOT NULL,
    stadium_id TEXT NOT NULL REFERENCES stadiums(stadium_id),
    surface_override TEXT,
    roof_status TEXT CHECK (roof_status IN ('open', 'closed', 'fixed', 'unknown') OR roof_status IS NULL),
    neutral_site INTEGER NOT NULL DEFAULT 0 CHECK (neutral_site IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'postponed', 'cancelled', 'final')),
    home_score INTEGER,
    away_score INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (home_team_id <> away_team_id),
    UNIQUE (sport, season, season_type, week, home_team_id, away_team_id, kickoff_time)
);

CREATE TABLE IF NOT EXISTS raw_ingests (
    ingest_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    endpoint_name TEXT NOT NULL,
    source_updated_at TEXT,
    collected_at TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    http_status INTEGER,
    UNIQUE (source_key, endpoint_name, content_sha256)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    collection_run_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    endpoint_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'partial', 'failed', 'skipped')),
    records_received INTEGER NOT NULL DEFAULT 0 CHECK (records_received >= 0),
    records_stored INTEGER NOT NULL DEFAULT 0 CHECK (records_stored >= 0),
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT,
    requests_remaining INTEGER,
    requests_used INTEGER,
    request_cost INTEGER,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_collection_runs_source_time
ON collection_runs(source_key, endpoint_name, started_at);

CREATE TABLE IF NOT EXISTS game_source_ids (
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_game_id TEXT NOT NULL,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (source_key, source_game_id),
    UNIQUE (source_key, game_id),
    CHECK (last_seen_at >= first_seen_at)
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
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
    CHECK (outcome_team_id <> opponent_team_id),
    UNIQUE (game_id, sportsbook_key, market_key, period, settlement_rule, outcome_team_id, spread_points, source_updated_at)
);

CREATE INDEX IF NOT EXISTS idx_odds_game_time
ON odds_snapshots(game_id, collected_at, sportsbook_key);

CREATE TABLE IF NOT EXISTS injury_snapshots (
    injury_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT REFERENCES games(game_id),
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    position TEXT,
    injury TEXT,
    practice_status TEXT,
    game_status TEXT,
    official INTEGER NOT NULL CHECK (official IN (0, 1)),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_updated_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    ingest_id INTEGER REFERENCES raw_ingests(ingest_id)
);

CREATE INDEX IF NOT EXISTS idx_injury_game_time
ON injury_snapshots(game_id, team_id, collected_at);

CREATE TABLE IF NOT EXISTS roster_snapshots (
    roster_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    position TEXT,
    roster_status TEXT NOT NULL,
    starter_status TEXT,
    depth_role TEXT,
    prior_snap_share REAL CHECK (prior_snap_share BETWEEN 0 AND 1 OR prior_snap_share IS NULL),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_updated_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    ingest_id INTEGER REFERENCES raw_ingests(ingest_id)
);

CREATE INDEX IF NOT EXISTS idx_roster_team_time
ON roster_snapshots(team_id, collected_at);

CREATE TABLE IF NOT EXISTS transaction_snapshots (
    transaction_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT,
    player_name TEXT NOT NULL,
    from_team_id TEXT REFERENCES teams(team_id),
    to_team_id TEXT REFERENCES teams(team_id),
    position TEXT,
    transaction_type TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    description TEXT NOT NULL,
    official INTEGER NOT NULL CHECK (official IN (0, 1)),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_updated_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    ingest_id INTEGER REFERENCES raw_ingests(ingest_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_date
ON transaction_snapshots(transaction_date, from_team_id, to_team_id);

CREATE TABLE IF NOT EXISTS data_files (
    data_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    dataset_name TEXT NOT NULL,
    season INTEGER,
    source_url TEXT NOT NULL,
    local_path TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    source_updated_at TEXT,
    collected_at TEXT NOT NULL,
    UNIQUE (source_key, dataset_name, season, content_sha256)
);

CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    sport TEXT NOT NULL CHECK (sport IN ('nfl', 'mlb')),
    player_name TEXT NOT NULL,
    primary_team_id TEXT REFERENCES teams(team_id),
    position TEXT,
    bats TEXT CHECK (bats IN ('L', 'R', 'S') OR bats IS NULL),
    throws TEXT CHECK (throws IN ('L', 'R') OR throws IS NULL),
    roster_status TEXT,
    verified_on TEXT,
    source_key TEXT REFERENCES sources(source_key),
    source_url TEXT
);

CREATE TABLE IF NOT EXISTS mlb_game_context (
    game_id TEXT PRIMARY KEY REFERENCES games(game_id),
    probable_home_pitcher_id TEXT REFERENCES players(player_id),
    probable_away_pitcher_id TEXT REFERENCES players(player_id),
    confirmed_home_pitcher_id TEXT REFERENCES players(player_id),
    confirmed_away_pitcher_id TEXT REFERENCES players(player_id),
    home_lineup_status TEXT,
    away_lineup_status TEXT,
    home_batting_order_json TEXT,
    away_batting_order_json TEXT,
    home_bullpen_available_json TEXT,
    away_bullpen_available_json TEXT,
    home_relievers_used_1d_json TEXT,
    away_relievers_used_1d_json TEXT,
    home_relievers_used_2d_json TEXT,
    away_relievers_used_2d_json TEXT,
    home_relievers_used_3d_json TEXT,
    away_relievers_used_3d_json TEXT,
    home_starter_recent_pitch_count INTEGER,
    away_starter_recent_pitch_count INTEGER,
    home_offense_vs_lhp REAL,
    home_offense_vs_rhp REAL,
    away_offense_vs_lhp REAL,
    away_offense_vs_rhp REAL,
    home_recent_5 REAL,
    away_recent_5 REAL,
    home_recent_10 REAL,
    away_recent_10 REAL,
    home_rest_days INTEGER,
    away_rest_days INTEGER,
    travel_notes TEXT,
    collected_at TEXT,
    source_notes_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    weather_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    forecast_for TEXT NOT NULL,
    temperature_f REAL,
    sustained_wind_mph REAL CHECK (sustained_wind_mph >= 0 OR sustained_wind_mph IS NULL),
    wind_gust_mph REAL CHECK (wind_gust_mph >= 0 OR wind_gust_mph IS NULL),
    precipitation_probability REAL CHECK (precipitation_probability BETWEEN 0 AND 1 OR precipitation_probability IS NULL),
    precipitation_type TEXT,
    alerts_json TEXT NOT NULL DEFAULT '[]',
    roof_status TEXT CHECK (roof_status IN ('open', 'closed', 'fixed', 'unknown')),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    source_updated_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    ingest_id INTEGER REFERENCES raw_ingests(ingest_id)
);

CREATE TABLE IF NOT EXISTS team_game_features (
    team_game_feature_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    team_id TEXT NOT NULL REFERENCES teams(team_id),
    opponent_team_id TEXT NOT NULL REFERENCES teams(team_id),
    feature_name TEXT NOT NULL,
    feature_value REAL NOT NULL,
    source_period_start TEXT,
    source_period_end TEXT NOT NULL,
    cutoff_time TEXT NOT NULL,
    calculation_version TEXT NOT NULL,
    source_key TEXT REFERENCES sources(source_key),
    CHECK (team_id <> opponent_team_id),
    UNIQUE (game_id, team_id, feature_name, cutoff_time, calculation_version)
);

CREATE TABLE IF NOT EXISTS model_runs (
    model_run_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    sport TEXT NOT NULL DEFAULT 'nfl' CHECK (sport IN ('nfl', 'mlb')),
    market_key TEXT NOT NULL DEFAULT 'spreads' CHECK (market_key IN ('spreads', 'moneylines')),
    model_version TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    data_version TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    training_cutoff TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    home_cover_probability REAL NOT NULL CHECK (home_cover_probability BETWEEN 0 AND 1),
    away_cover_probability REAL NOT NULL CHECK (away_cover_probability BETWEEN 0 AND 1),
    push_probability REAL NOT NULL CHECK (push_probability BETWEEN 0 AND 1),
    uncertainty_margin REAL NOT NULL CHECK (uncertainty_margin BETWEEN 0 AND 1),
    market_spread_points REAL CHECK (market_spread_points BETWEEN -40 AND 40 OR market_spread_points IS NULL),
    oos_decision_count INTEGER NOT NULL DEFAULT 0 CHECK (oos_decision_count >= 0),
    calibration_status TEXT NOT NULL DEFAULT 'untested' CHECK (calibration_status IN ('untested', 'acceptable', 'unacceptable')),
    clv_status TEXT NOT NULL DEFAULT 'unknown' CHECK (clv_status IN ('unknown', 'stable', 'favorable', 'unfavorable')),
    status TEXT NOT NULL CHECK (status IN ('test_only', 'rejected')),
    created_at TEXT NOT NULL,
    CHECK (ABS((home_cover_probability + away_cover_probability + push_probability) - 1.0) < 0.000001),
    CHECK (training_cutoff <= prediction_time)
);

CREATE TABLE IF NOT EXISTS historical_training_games (
    source_game_id TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL CHECK (week BETWEEN 1 AND 25),
    kickoff_time TEXT NOT NULL,
    home_team_id TEXT NOT NULL REFERENCES teams(team_id),
    away_team_id TEXT NOT NULL REFERENCES teams(team_id),
    home_score INTEGER NOT NULL CHECK (home_score >= 0),
    away_score INTEGER NOT NULL CHECK (away_score >= 0),
    home_spread_points REAL NOT NULL CHECK (home_spread_points BETWEEN -40 AND 40),
    home_spread_decimal_odds REAL CHECK (home_spread_decimal_odds > 1 OR home_spread_decimal_odds IS NULL),
    away_spread_decimal_odds REAL CHECK (away_spread_decimal_odds > 1 OR away_spread_decimal_odds IS NULL),
    home_market_no_vig_probability REAL CHECK (home_market_no_vig_probability BETWEEN 0 AND 1 OR home_market_no_vig_probability IS NULL),
    home_rest_days INTEGER CHECK (home_rest_days >= 0 OR home_rest_days IS NULL),
    away_rest_days INTEGER CHECK (away_rest_days >= 0 OR away_rest_days IS NULL),
    home_qb_id TEXT,
    away_qb_id TEXT,
    home_coach TEXT,
    away_coach TEXT,
    line_type TEXT NOT NULL CHECK (line_type = 'nflverse_closing'),
    source_updated_at TEXT,
    collected_at TEXT NOT NULL,
    CHECK (home_team_id <> away_team_id),
    UNIQUE (season, week, home_team_id, away_team_id)
);

CREATE INDEX IF NOT EXISTS idx_historical_training_season_time
ON historical_training_games(season, kickoff_time);

CREATE TABLE IF NOT EXISTS model_artifacts (
    model_version TEXT PRIMARY KEY,
    feature_version TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    training_cutoff TEXT NOT NULL,
    training_count INTEGER NOT NULL CHECK (training_count >= 0),
    validation_count INTEGER NOT NULL CHECK (validation_count >= 0),
    locked_test_count INTEGER NOT NULL CHECK (locked_test_count >= 0),
    coefficients_json TEXT NOT NULL,
    calibration_json TEXT NOT NULL,
    push_rates_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('acceptable', 'rejected'))
);

CREATE TABLE IF NOT EXISTS evaluation_contexts (
    evaluation_context_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    as_of TEXT NOT NULL,
    research_complete INTEGER NOT NULL CHECK (research_complete IN (0, 1)),
    market_match_confirmed INTEGER NOT NULL CHECK (market_match_confirmed IN (0, 1)),
    material_information_complete INTEGER NOT NULL CHECK (material_information_complete IN (0, 1)),
    information_quality TEXT NOT NULL CHECK (information_quality IN ('full_official', 'complete_minor_uncertainty', 'projected', 'missing_or_conflicting')),
    conflict_status TEXT NOT NULL CHECK (conflict_status IN ('clear', 'conflict', 'unknown')),
    exposure_status TEXT NOT NULL CHECK (exposure_status IN ('clear', 'blocked', 'unknown')),
    stability_status TEXT NOT NULL CHECK (stability_status IN ('robust', 'conditional', 'fragile', 'unknown')),
    manual_approval_status TEXT NOT NULL CHECK (manual_approval_status IN ('pending', 'approved', 'rejected')),
    source_notes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE (game_id, as_of)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    model_run_id TEXT NOT NULL REFERENCES model_runs(model_run_id),
    selected_odds_snapshot_id INTEGER REFERENCES odds_snapshots(odds_snapshot_id),
    selected_team_id TEXT REFERENCES teams(team_id),
    spread_points REAL,
    decimal_odds REAL,
    break_even_probability REAL,
    no_vig_probability REAL,
    conservative_probability REAL,
    push_probability REAL,
    probability_edge REAL,
    ev_per_dollar REAL,
    minimum_acceptable_decimal_odds REAL,
    gate_results_json TEXT NOT NULL,
    pick_strength_score INTEGER CHECK (pick_strength_score BETWEEN 0 AND 100 OR pick_strength_score IS NULL),
    confidence_label TEXT,
    decision_type TEXT NOT NULL CHECK (decision_type IN ('test_only_selection', 'pass')),
    pass_reason TEXT,
    manual_review_status TEXT NOT NULL CHECK (manual_review_status IN ('pending', 'approved_for_tracking', 'rejected')),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    loss_reasons_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    CHECK (
        (decision_type = 'pass')
        OR (selected_team_id IS NOT NULL AND selected_odds_snapshot_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
    home_score INTEGER NOT NULL CHECK (home_score >= 0),
    away_score INTEGER NOT NULL CHECK (away_score >= 0),
    cover_result TEXT NOT NULL CHECK (cover_result IN ('win', 'loss', 'push', 'pass')),
    push_status INTEGER NOT NULL CHECK (push_status IN (0, 1)),
    closing_spread REAL,
    closing_decimal_odds REAL,
    closing_source TEXT,
    clv_value REAL,
    paper_profit_loss REAL NOT NULL,
    settled_at TEXT NOT NULL
);

-- Phase 5 immutable paper-tracking ledger.  Passes are first-class decisions,
-- so this table intentionally permits a missing model run and missing price.
CREATE TABLE IF NOT EXISTS paper_decisions (
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
    CHECK (
        decision_type = 'pass'
        OR (selected_team_id IS NOT NULL AND selected_odds_snapshot_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_paper_decisions_time
ON paper_decisions(countable_oos, decision_time);

CREATE TABLE IF NOT EXISTS paper_results (
    paper_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_decision_id TEXT NOT NULL UNIQUE REFERENCES paper_decisions(paper_decision_id),
    home_score INTEGER NOT NULL CHECK (home_score >= 0),
    away_score INTEGER NOT NULL CHECK (away_score >= 0),
    cover_result TEXT NOT NULL CHECK (cover_result IN ('win', 'loss', 'push', 'pass')),
    push_status INTEGER NOT NULL CHECK (push_status IN (0, 1)),
    entry_spread REAL,
    entry_decimal_odds REAL,
    closing_spread REAL,
    closing_decimal_odds REAL,
    closing_sportsbook_key TEXT,
    closing_snapshot_time TEXT,
    consensus_closing_probability REAL,
    line_clv_points REAL,
    exact_line_price_clv_probability REAL,
    close_comparison TEXT CHECK (close_comparison IN ('better', 'equal', 'worse', 'unavailable')),
    paper_profit_loss REAL NOT NULL,
    settled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_checks (
    source_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL REFERENCES games(game_id),
    decision_id TEXT REFERENCES decisions(decision_id),
    source_key TEXT NOT NULL REFERENCES sources(source_key),
    check_type TEXT NOT NULL,
    access_status TEXT NOT NULL CHECK (access_status IN ('confirmed', 'unavailable', 'conflict', 'stale', 'not_required')),
    source_updated_at TEXT,
    checked_at TEXT NOT NULL,
    conflict_details TEXT,
    missing_information TEXT,
    reviewer_notes TEXT,
    source_url TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    previous_value_json TEXT,
    new_value_json TEXT,
    responsible_actor TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS prevent_future_odds_decision
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
END;

CREATE TRIGGER IF NOT EXISTS prevent_future_features_insert
BEFORE INSERT ON model_runs
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM team_game_features f
        WHERE f.game_id = NEW.game_id AND f.cutoff_time > NEW.prediction_time
    ) THEN RAISE(ABORT, 'future feature exists beyond prediction time') END;
END;
