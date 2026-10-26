"""Runtime schema and records for our multi-sport test-only system.

The original NFL/MLB tables remain untouched.  These tables support model training,
predictions, paper decisions, settlement, CLV, validation, ranking, and future manual
approval for NBA, WNBA, Premier League, UFC/MMA, ATP, and WTA.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from .quota_budget import ensure_quota_schema
from .sports_registry import get_sport_market, validation_bucket


UTC = timezone.utc
SPORTS = ("nba", "wnba", "epl", "mma", "atp", "wta")
RESEARCH_CATEGORIES = (
    "official_status",
    "injury_availability",
    "lineup_or_participants",
    "statistics_analytics",
    "transactions_or_changes",
    "current_odds",
    "weather_environment",
    "rest_travel",
    "weigh_in_status",
)


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a time zone")
    return value.astimezone(UTC).isoformat()


def _ensure_research_schema_v2(connection: sqlite3.Connection) -> None:
    """Migrate the additive research table without touching NFL or MLB tables."""
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='multisport_research_checks'"
    ).fetchone()
    if row is None:
        return
    sql = str(row[0] or "")
    columns = {
        str(column[1])
        for column in connection.execute("PRAGMA table_info(multisport_research_checks)")
    }
    if "weather_environment" in sql and {"evidence_json", "evidence_hash"} <= columns:
        return

    allowed = ",".join(f"'{category}'" for category in RESEARCH_CATEGORIES)
    connection.execute("ALTER TABLE multisport_research_checks RENAME TO multisport_research_checks_legacy")
    connection.execute(
        f"""
        CREATE TABLE multisport_research_checks (
            research_check_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            category TEXT NOT NULL CHECK (category IN ({allowed})),
            source_key TEXT NOT NULL,
            source_url TEXT,
            official INTEGER NOT NULL CHECK (official IN (0,1)),
            verification_status TEXT NOT NULL CHECK (
                verification_status IN ('verified','conditional','unavailable','conflict')
            ),
            source_updated_at TEXT,
            collected_at TEXT NOT NULL,
            notes TEXT,
            evidence_json TEXT NOT NULL DEFAULT '{{}}',
            evidence_hash TEXT,
            UNIQUE (event_id, category, source_key, collected_at)
        )
        """
    )
    legacy_columns = {
        str(column[1])
        for column in connection.execute("PRAGMA table_info(multisport_research_checks_legacy)")
    }
    evidence_json = "evidence_json" if "evidence_json" in legacy_columns else "'{}'"
    evidence_hash = "evidence_hash" if "evidence_hash" in legacy_columns else "NULL"
    connection.execute(
        f"""
        INSERT INTO multisport_research_checks (
            research_check_id, event_id, sport, category, source_key, source_url,
            official, verification_status, source_updated_at, collected_at, notes,
            evidence_json, evidence_hash
        )
        SELECT research_check_id, event_id, sport, category, source_key, source_url,
               official, verification_status, source_updated_at, collected_at, notes,
               {evidence_json}, {evidence_hash}
        FROM multisport_research_checks_legacy
        """
    )
    connection.execute("DROP TABLE multisport_research_checks_legacy")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_multisport_research_event
        ON multisport_research_checks(event_id, category, collected_at)
        """
    )
    connection.commit()


def ensure_runtime_schema(connection: sqlite3.Connection) -> None:
    """Create additive tables needed after the Phase 2 collector foundation."""
    _ensure_research_schema_v2(connection)
    ensure_quota_schema(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS multisport_feature_snapshots (
            feature_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            feature_name TEXT NOT NULL,
            feature_value REAL NOT NULL,
            source_key TEXT,
            source_updated_at TEXT,
            collected_at TEXT NOT NULL,
            cutoff_time TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            UNIQUE(event_id, feature_name, cutoff_time, feature_version),
            CHECK(source_updated_at IS NULL OR source_updated_at <= cutoff_time),
            CHECK(collected_at <= cutoff_time)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_features_event_cutoff
        ON multisport_feature_snapshots(event_id, cutoff_time, feature_version);

        CREATE TABLE IF NOT EXISTS multisport_training_examples (
            training_example_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            provider_sport_key TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            event_time TEXT NOT NULL,
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            features_json TEXT NOT NULL,
            outcome_role TEXT NOT NULL CHECK (outcome_role IN ('home','away','draw','push','void')),
            source_cutoff TEXT NOT NULL,
            result_source_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(sport, provider_sport_key, source_event_id, market_key, period, feature_version),
            CHECK(source_cutoff <= event_time)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_training_time
        ON multisport_training_examples(sport, market_key, event_time);

        CREATE TABLE IF NOT EXISTS multisport_model_artifacts (
            model_version TEXT PRIMARY KEY,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            feature_version TEXT NOT NULL,
            algorithm TEXT NOT NULL CHECK (algorithm IN ('binary_logistic','softmax')),
            feature_names_json TEXT NOT NULL,
            parameters_json TEXT NOT NULL,
            trained_at TEXT NOT NULL,
            training_cutoff TEXT NOT NULL,
            training_count INTEGER NOT NULL CHECK (training_count >= 0),
            holdout_count INTEGER NOT NULL CHECK (holdout_count >= 0),
            holdout_log_loss REAL,
            holdout_brier REAL,
            calibration_error REAL,
            base_uncertainty REAL NOT NULL CHECK (base_uncertainty BETWEEN 0 AND 0.25),
            data_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('test_only','rejected')),
            notes_json TEXT NOT NULL DEFAULT '[]'
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_models_sport_market
        ON multisport_model_artifacts(sport, market_key, trained_at);

        CREATE TABLE IF NOT EXISTS multisport_predictions (
            prediction_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            model_version TEXT NOT NULL REFERENCES multisport_model_artifacts(model_version),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            prediction_time TEXT NOT NULL,
            feature_cutoff TEXT NOT NULL,
            probabilities_json TEXT NOT NULL,
            uncertainty_margin REAL NOT NULL CHECK (uncertainty_margin BETWEEN 0 AND 0.25),
            status TEXT NOT NULL CHECK (status IN ('test_only','rejected')),
            created_at TEXT NOT NULL,
            UNIQUE(event_id, model_version, prediction_time),
            CHECK(feature_cutoff <= prediction_time)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_predictions_event
        ON multisport_predictions(event_id, prediction_time);

        CREATE TABLE IF NOT EXISTS multisport_settlement_rules (
            sportsbook_key TEXT NOT NULL,
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            rule_key TEXT NOT NULL,
            rule_status TEXT NOT NULL CHECK (rule_status IN ('verified','pending','retired')),
            source_url TEXT NOT NULL,
            verified_at TEXT,
            notes TEXT NOT NULL,
            PRIMARY KEY(sportsbook_key, sport, market_key, period)
        );

        CREATE TABLE IF NOT EXISTS multisport_paper_decisions (
            paper_decision_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES multisport_events(event_id),
            prediction_id TEXT REFERENCES multisport_predictions(prediction_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            outcome_role TEXT,
            outcome_participant_id TEXT REFERENCES multisport_participants(participant_id),
            sportsbook_key TEXT,
            odds_snapshot_id INTEGER REFERENCES multisport_odds_snapshots(odds_snapshot_id),
            points REAL,
            decimal_odds REAL,
            check_time TEXT NOT NULL,
            break_even_probability REAL,
            market_consensus_probability REAL,
            model_probability REAL,
            uncertainty_margin REAL,
            conservative_probability REAL,
            probability_edge REAL,
            ev_per_dollar REAL,
            minimum_acceptable_decimal_odds REAL,
            pick_strength_score INTEGER CHECK (pick_strength_score BETWEEN 0 AND 100 OR pick_strength_score IS NULL),
            confidence_label TEXT,
            decision_type TEXT NOT NULL CHECK (decision_type IN ('official_pick','conditional_pick','test_only_selection','pass')),
            gate_results_json TEXT NOT NULL,
            reasons_json TEXT NOT NULL DEFAULT '[]',
            loss_reasons_json TEXT NOT NULL DEFAULT '[]',
            pass_reason TEXT,
            manual_approval_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (manual_approval_status IN ('pending','approved_for_tracking','approved_for_manual_wager','rejected')),
            countable_oos INTEGER NOT NULL DEFAULT 0 CHECK (countable_oos IN (0,1)),
            model_version TEXT,
            rule_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(event_id, market_key, period),
            CHECK(
                decision_type = 'pass'
                OR (outcome_role IS NOT NULL AND odds_snapshot_id IS NOT NULL AND decimal_odds IS NOT NULL)
            )
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_paper_decisions_time
        ON multisport_paper_decisions(sport, decision_type, check_time);

        CREATE TABLE IF NOT EXISTS multisport_results (
            result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE REFERENCES multisport_events(event_id),
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            home_score REAL,
            away_score REAL,
            winner_role TEXT CHECK (winner_role IN ('home','away','draw','void') OR winner_role IS NULL),
            result_status TEXT NOT NULL CHECK (result_status IN ('final','cancelled','postponed','no_contest','walkover','retirement')),
            source_key TEXT NOT NULL,
            source_updated_at TEXT NOT NULL,
            collected_at TEXT NOT NULL,
            raw_result_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS multisport_paper_results (
            paper_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_decision_id TEXT NOT NULL UNIQUE REFERENCES multisport_paper_decisions(paper_decision_id),
            grading_result TEXT NOT NULL CHECK (grading_result IN ('win','loss','push','void','pass','unsettled')),
            paper_profit_loss REAL NOT NULL DEFAULT 0,
            closing_decimal_odds REAL,
            closing_probability REAL,
            entry_probability REAL,
            probability_clv REAL,
            close_comparison TEXT CHECK (close_comparison IN ('better','equal','worse','unavailable') OR close_comparison IS NULL),
            settled_at TEXT NOT NULL,
            settlement_notes TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS multisport_result_notifications (
            paper_decision_id TEXT NOT NULL REFERENCES multisport_paper_decisions(paper_decision_id),
            channel TEXT NOT NULL,
            notified_at TEXT NOT NULL,
            source_workflow_run_id TEXT NOT NULL,
            PRIMARY KEY (paper_decision_id, channel)
        );

        CREATE INDEX IF NOT EXISTS idx_multisport_result_notifications_channel
        ON multisport_result_notifications(channel, notified_at);

        CREATE TABLE IF NOT EXISTS multisport_validation_metrics (
            validation_bucket TEXT PRIMARY KEY,
            completed_count INTEGER NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
            wins INTEGER NOT NULL DEFAULT 0 CHECK (wins >= 0),
            losses INTEGER NOT NULL DEFAULT 0 CHECK (losses >= 0),
            pushes INTEGER NOT NULL DEFAULT 0 CHECK (pushes >= 0),
            voids INTEGER NOT NULL DEFAULT 0 CHECK (voids >= 0),
            roi REAL,
            average_clv REAL,
            brier_score REAL,
            calibration_error REAL,
            max_drawdown REAL,
            calibration_status TEXT NOT NULL DEFAULT 'untested'
                CHECK (calibration_status IN ('untested','acceptable','unacceptable')),
            clv_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (clv_status IN ('unknown','stable','favorable','unfavorable')),
            review_status TEXT NOT NULL DEFAULT 'collecting'
                CHECK (review_status IN ('collecting','ready_for_review','reviewed_pass','reviewed_fail')),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS multisport_model_approvals (
            sport TEXT NOT NULL CHECK (sport IN ('nba','wnba','epl','mma','atp','wta')),
            market_key TEXT NOT NULL,
            period TEXT NOT NULL,
            model_version TEXT NOT NULL REFERENCES multisport_model_artifacts(model_version),
            approval_status TEXT NOT NULL DEFAULT 'test_only'
                CHECK (approval_status IN ('test_only','eligible_for_manual_approval','approved','rejected')),
            approved_by TEXT,
            approved_at TEXT,
            notes TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(sport, market_key, period)
        );

        CREATE TABLE IF NOT EXISTS multisport_parlays (
            parlay_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            leg_count INTEGER NOT NULL CHECK (leg_count BETWEEN 2 AND 20),
            decimal_odds REAL,
            joint_probability REAL,
            ev_per_dollar REAL,
            correlation_method TEXT NOT NULL,
            decision_type TEXT NOT NULL CHECK (decision_type IN ('test_only_selection','pass')),
            pass_reason TEXT,
            manual_approval_status TEXT NOT NULL DEFAULT 'pending'
                CHECK (manual_approval_status IN ('pending','approved_for_tracking','rejected')),
            legs_json TEXT NOT NULL
        );
        """
    )
    connection.commit()


def seed_settlement_rules(connection: sqlite3.Connection, *, verified_at: datetime | None = None) -> None:
    """Seed only exact sport/market settlement rules backed by reviewed sources.

    Every verified row is scoped to one sportsbook, sport, market, and period. A
    missing bookmaker-specific rule remains fail-closed rather than inheriting another
    book's settlement behavior.
    """
    ensure_runtime_schema(connection)
    stamp = _iso(verified_at)
    rows = (
        (
            "hardrockbet", "nba", "spreads", "full_game",
            "basketball_full_game_spread_overtime_push_refund", "verified",
            "https://www.hardrock.bet/sportsbook/basketball/nba",
            stamp,
            "Full-game NBA spreads use the final game score; whole-number ties push/refund and overtime is normally included.",
        ),
        (
            "hardrockbet", "wnba", "spreads", "full_game",
            "basketball_full_game_spread_overtime_push_refund", "verified",
            "https://www.hardrock.bet/sportsbook/basketball/wnba/",
            stamp,
            "Full-game WNBA spreads normally include overtime; whole-number spread ties push/refund.",
        ),
        (
            "hardrockbet", "epl", "three_way_moneyline", "full_game",
            "soccer_1x2_90_plus_stoppage_no_extra_time", "verified",
            "https://www.hardrock.bet/sportsbook/3-way-moneyline/",
            stamp,
            "Three-way soccer moneyline settles after 90 minutes plus stoppage time; extra time and penalties do not count.",
        ),
        (
            "hardrockbet", "mma", "moneylines", "full_fight",
            "book_mma_two_way_draw_push_no_contest_void", "verified",
            "https://casino.hardrock.com/bristol/-/media/project/shrss/hri/casinos/hard-rock/bristol/casino/sportsbook/varetailhouserules_2025.pdf",
            stamp,
            "Reviewed sportsbook MMA rules: no contest cancels/refunds fight bets unless already determined; a two-fighter Fight Winner draw refunds either fighter. Current sportsbook guidance independently confirms UFC two-way moneyline draws push.",
        ),
        (
            "hardrockbet", "atp", "match_moneyline", "full_match",
            "book_tennis_first_set_retirement_action", "verified",
            "https://www.hardrock.bet/all-house-rules/",
            stamp,
            "Sportsbook tennis moneyline: after the first set is completed, the player advancing is winner and the player not advancing is loser. Before first-set completion the retirement is void; a no-start/walkover is void. Explicit first-set evidence is required.",
        ),
        (
            "hardrockbet", "wta", "match_moneyline", "full_match",
            "book_tennis_first_set_retirement_action", "verified",
            "https://www.hardrock.bet/all-house-rules/",
            stamp,
            "Sportsbook tennis moneyline: after the first set is completed, the player advancing is winner and the player not advancing is loser. Before first-set completion the retirement is void; a no-start/walkover is void. Explicit first-set evidence is required.",
        ),
        (
            "draftkings", "atp", "match_moneyline", "full_match",
            "draftkings_tennis_match_moneyline_interrupted_advancer", "verified",
            "https://sportsbook.draftkings.com/help/sport-rules/tennis",
            stamp,
            "DraftKings ATP match moneyline: after at least one completed point, the official winner/advancer is won and other selections are void when a match ends early; no completed point or walkover voids match bets.",
        ),
        (
            "draftkings", "wta", "match_moneyline", "full_match",
            "draftkings_tennis_match_moneyline_interrupted_advancer", "verified",
            "https://sportsbook.draftkings.com/help/sport-rules/tennis",
            stamp,
            "DraftKings WTA match moneyline: after at least one completed point, the official winner/advancer is won and other selections are void when a match ends early; no completed point or walkover voids match bets.",
        ),
    )
    connection.executemany(
        """
        INSERT INTO multisport_settlement_rules (
            sportsbook_key, sport, market_key, period, rule_key, rule_status,
            source_url, verified_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sportsbook_key, sport, market_key, period) DO UPDATE SET
            rule_key=excluded.rule_key,
            rule_status=excluded.rule_status,
            source_url=excluded.source_url,
            verified_at=excluded.verified_at,
            notes=excluded.notes
        """,
        rows,
    )
    connection.commit()


def record_feature(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    sport: str,
    feature_name: str,
    feature_value: float,
    cutoff_time: datetime,
    feature_version: str,
    collected_at: datetime | None = None,
    source_key: str | None = None,
    source_updated_at: datetime | None = None,
) -> int:
    ensure_runtime_schema(connection)
    collected = collected_at or cutoff_time
    if collected > cutoff_time:
        raise ValueError("Feature collection time cannot follow the cutoff")
    if source_updated_at and source_updated_at > cutoff_time:
        raise ValueError("Feature source update cannot follow the cutoff")
    cursor = connection.execute(
        """
        INSERT INTO multisport_feature_snapshots (
            event_id, sport, feature_name, feature_value, source_key,
            source_updated_at, collected_at, cutoff_time, feature_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, sport, feature_name, float(feature_value), source_key,
            _iso(source_updated_at) if source_updated_at else None,
            _iso(collected), _iso(cutoff_time), feature_version,
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def feature_vector(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    cutoff_time: datetime,
    feature_version: str,
) -> dict[str, float]:
    ensure_runtime_schema(connection)
    rows = connection.execute(
        """
        SELECT feature_name, feature_value, cutoff_time
        FROM multisport_feature_snapshots
        WHERE event_id=? AND feature_version=? AND cutoff_time<=?
        ORDER BY cutoff_time DESC, feature_snapshot_id DESC
        """,
        (event_id, feature_version, _iso(cutoff_time)),
    ).fetchall()
    latest: dict[str, float] = {}
    for row in rows:
        latest.setdefault(str(row["feature_name"]), float(row["feature_value"]))
    return latest


def record_training_example(
    connection: sqlite3.Connection,
    *,
    sport: str,
    provider_sport_key: str,
    source_event_id: str,
    event_time: datetime,
    market_key: str,
    period: str,
    feature_version: str,
    features: dict[str, float],
    outcome_role: str,
    source_cutoff: datetime,
    result_source_key: str,
    created_at: datetime | None = None,
) -> int:
    ensure_runtime_schema(connection)
    if source_cutoff > event_time:
        raise ValueError("Training features cannot use information after event time")
    if outcome_role not in {"home", "away", "draw", "push", "void"}:
        raise ValueError("Unsupported training outcome")
    spec = get_sport_market(sport)
    if market_key != spec.internal_market_key or period != spec.period:
        raise ValueError("Training example does not match controlled market")
    payload = {str(k): float(v) for k, v in sorted(features.items())}
    cursor = connection.execute(
        """
        INSERT INTO multisport_training_examples (
            sport, provider_sport_key, source_event_id, event_time, market_key,
            period, feature_version, features_json, outcome_role, source_cutoff,
            result_source_key, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sport, provider_sport_key, source_event_id, _iso(event_time), market_key,
            period, feature_version, json.dumps(payload, sort_keys=True), outcome_role,
            _iso(source_cutoff), result_source_key, _iso(created_at),
        ),
    )
    connection.commit()
    return int(cursor.lastrowid)


def validation_count(connection: sqlite3.Connection, sport: str) -> int:
    ensure_runtime_schema(connection)
    bucket = validation_bucket(sport)
    row = connection.execute(
        "SELECT completed_count FROM multisport_validation_metrics WHERE validation_bucket=?",
        (bucket,),
    ).fetchone()
    return int(row[0]) if row else 0


def approval_status(connection: sqlite3.Connection, sport: str, model_version: str) -> str:
    ensure_runtime_schema(connection)
    spec = get_sport_market(sport)
    row = connection.execute(
        """
        SELECT approval_status FROM multisport_model_approvals
        WHERE sport=? AND market_key=? AND period=? AND model_version=?
        """,
        (sport, spec.internal_market_key, spec.period, model_version),
    ).fetchone()
    return str(row[0]) if row else "test_only"


def set_manual_model_approval(
    connection: sqlite3.Connection,
    *,
    sport: str,
    model_version: str,
    status: str,
    approved_by: str,
    notes: str = "",
    approved_at: datetime | None = None,
) -> None:
    """Record a human model review; never called automatically by evaluation code."""
    ensure_runtime_schema(connection)
    if status not in {"test_only", "eligible_for_manual_approval", "approved", "rejected"}:
        raise ValueError("Invalid model approval status")
    spec = get_sport_market(sport)
    count = validation_count(connection, sport)
    metrics = connection.execute(
        "SELECT * FROM multisport_validation_metrics WHERE validation_bucket=?",
        (validation_bucket(sport),),
    ).fetchone()
    if status == "approved":
        if count < spec.validation_target:
            raise ValueError("Cannot approve a model before its validation target is reached")
        if metrics is None or metrics["calibration_status"] != "acceptable":
            raise ValueError("Cannot approve a model without acceptable calibration")
        if metrics["review_status"] != "reviewed_pass":
            raise ValueError("Cannot approve a model before manual validation review passes")
    connection.execute(
        """
        INSERT INTO multisport_model_approvals (
            sport, market_key, period, model_version, approval_status,
            approved_by, approved_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sport, market_key, period) DO UPDATE SET
            model_version=excluded.model_version,
            approval_status=excluded.approval_status,
            approved_by=excluded.approved_by,
            approved_at=excluded.approved_at,
            notes=excluded.notes
        """,
        (
            sport, spec.internal_market_key, spec.period, model_version, status,
            approved_by, _iso(approved_at) if status == "approved" else None, notes,
        ),
    )
    connection.commit()


def runtime_table_names() -> set[str]:
    return {
        "multisport_feature_snapshots",
        "multisport_training_examples",
        "multisport_model_artifacts",
        "multisport_predictions",
        "multisport_settlement_rules",
        "multisport_paper_decisions",
        "multisport_results",
        "multisport_paper_results",
        "multisport_result_notifications",
        "multisport_validation_metrics",
        "multisport_model_approvals",
        "multisport_parlays",
        "provider_daily_credit_budgets",
        "provider_poll_log",
    }
