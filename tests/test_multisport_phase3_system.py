import json
import unittest
from datetime import datetime, timedelta, timezone

from sports_analytics_pipeline.collectors.multisport_scores import parse_scores
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_evaluation import (
    assess_exposure,
    build_three_leg_parlay,
    decision_presentation,
    evaluate_event,
    letter_grade,
)
from sports_analytics_pipeline.multisport_features import (
    EPL_BASELINE_FEATURE_VERSION,
    EPL_BASELINE_FEATURES,
    FEATURE_VERSION,
    required_feature_names,
    validate_feature_payload,
)
from sports_analytics_pipeline.multisport_history import import_history_rows, validate_history_row
from sports_analytics_pipeline.multisport_modeling import train_model
from sports_analytics_pipeline.multisport_operations import bootstrap, status_report
from sports_analytics_pipeline.multisport_research import record_research_check, required_categories, research_sources
from sports_analytics_pipeline.multisport_research_pipeline import ResearchObservation, persist_observation
from sports_analytics_pipeline.multisport_results import record_result, settle_decision
from sports_analytics_pipeline.multisport_runtime import (
    record_feature,
    record_training_example,
    runtime_table_names,
    set_manual_model_approval,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 17, 22, 0, tzinfo=UTC)
START = NOW + timedelta(hours=1)


def nba_features(index=0):
    return {
        "market_spread_points": -3.0,
        "market_home_no_vig": 0.50 + ((index % 5) - 2) * 0.003,
        "net_rating_diff": ((index % 13) - 6) * 0.8,
        "recent5_margin_diff": ((index % 9) - 4) * 0.7,
        "recent10_margin_diff": ((index % 11) - 5) * 0.5,
        "rest_days_diff": float((index % 5) - 2),
        "lineup_strength_diff": ((index % 7) - 3) * 0.4,
        "availability_impact_diff": ((index % 6) - 3) * 0.3,
        "travel_load_diff": ((index % 4) - 2) * 0.2,
    }


def epl_baseline_features(index=0):
    home = 0.42 + ((index % 5) - 2) * 0.005
    draw = 0.27
    away = 1.0 - home - draw
    return {
        "market_home_no_vig": home,
        "market_draw_no_vig": draw,
        "market_away_no_vig": away,
        "recent5_points_diff": float((index % 11) - 5),
        "home_away_strength_diff": ((index % 9) - 4) * 0.25,
        "rest_days_diff": float((index % 5) - 2),
        "fixture_congestion_diff": float((index % 4) - 2),
    }


class MultiSportSystemTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        bootstrap(self.db, as_of=NOW)

    def tearDown(self):
        self.db.close()

    def participant(self, pid, sid, name):
        self.db.execute(
            """
            INSERT INTO multisport_participants (
                participant_id, sport, provider_sport_key, source_participant_id,
                full_name, participant_type, first_seen_at, last_seen_at, active_status
            ) VALUES (?, 'nba', 'basketball_nba', ?, ?, 'team', ?, ?, 'provider_listed')
            """,
            (pid, sid, name, NOW.isoformat(), NOW.isoformat()),
        )

    def event(self, suffix="1"):
        home = f"nba:home-{suffix}"
        away = f"nba:away-{suffix}"
        event_id = f"nba:basketball_nba:event-{suffix}"
        self.participant(home, f"home-{suffix}", f"Home {suffix}")
        self.participant(away, f"away-{suffix}", f"Away {suffix}")
        self.db.execute(
            """
            INSERT INTO multisport_events (
                event_id, sport, provider_sport_key, source_event_id, commence_time,
                home_participant_id, away_participant_id, status,
                first_seen_at, last_seen_at, test_only
            ) VALUES (?, 'nba', 'basketball_nba', ?, ?, ?, ?, 'scheduled', ?, ?, 1)
            """,
            (event_id, f"event-{suffix}", START.isoformat(), home, away, NOW.isoformat(), NOW.isoformat()),
        )
        self.db.commit()
        return event_id, home, away

    def odds(
        self,
        event_id,
        home,
        away,
        home_price=2.10,
        away_price=1.80,
        book="hardrockbet",
        title="Primary Sportsbook",
    ):
        ids = {}
        for role, participant, points, price in (
            ("home", home, -3.0, home_price),
            ("away", away, 3.0, away_price),
        ):
            cur = self.db.execute(
                """
                INSERT INTO multisport_odds_snapshots (
                    event_id, sport, provider_sport_key, sportsbook_key, sportsbook_title,
                    market_key, provider_market_key, period, settlement_rule,
                    outcome_participant_id, outcome_name, outcome_role, points,
                    decimal_odds, source_updated_at, collected_at, market_status, test_only
                ) VALUES (?, 'nba', 'basketball_nba', ?, ?,
                          'spreads', 'spreads', 'full_game',
                          'basketball_full_game_spread_overtime_push_refund',
                          ?, ?, ?, ?, ?, ?, ?, 'open', 1)
                """,
                (
                    event_id, book, title, participant, participant, role, points, price,
                    NOW.isoformat(), NOW.isoformat(),
                ),
            )
            ids[role] = int(cur.lastrowid)
        self.db.commit()
        return ids

    def model(self, version="nba-test-model"):
        names = list(required_feature_names("nba"))
        self.db.execute(
            """
            INSERT INTO multisport_model_artifacts (
                model_version, sport, market_key, period, feature_version, algorithm,
                feature_names_json, parameters_json, trained_at, training_cutoff,
                training_count, holdout_count, holdout_log_loss, holdout_brier,
                calibration_error, base_uncertainty, data_hash, status, notes_json
            ) VALUES (?, 'nba', 'spreads', 'full_game', ?, 'binary_logistic', ?, ?, ?, ?,
                      100, 20, 0.65, 0.22, 0.02, 0.02, 'testhash', 'test_only', '[]')
            """,
            (
                version,
                FEATURE_VERSION,
                json.dumps(names),
                json.dumps({
                    "labels": ["home", "away"],
                    "weights": [0.0] * (len(names) + 1),
                    "means": [0.0] * len(names),
                    "scales": [1.0] * len(names),
                    "push_rate": 0.0,
                    "feature_names": names,
                }),
                (NOW - timedelta(days=1)).isoformat(),
                (NOW - timedelta(days=2)).isoformat(),
            ),
        )
        self.db.commit()
        return version

    def verify_settlement(self, book):
        self.db.execute(
            """
            INSERT INTO multisport_settlement_rules (
                sportsbook_key, sport, market_key, period, rule_key, rule_status,
                source_url, verified_at, notes
            ) VALUES (?, 'nba', 'spreads', 'full_game',
                      'basketball_full_game_spread_overtime_push_refund', 'verified',
                      'https://example.test/verified-rule', ?, 'verified test rule')
            """,
            (book, NOW.isoformat()),
        )
        self.db.commit()

    def research(self, event_id):
        by_category = {}
        for source in research_sources("nba"):
            by_category.setdefault(source.category, source)
        for category in required_categories("nba"):
            source = by_category[category]
            record_research_check(
                self.db,
                event_id=event_id,
                sport="nba",
                category=category,
                source_key=source.source_key,
                verification_status="verified",
                official=source.official,
                source_url=source.url,
                source_updated_at=NOW,
                collected_at=NOW,
                notes="verified test evidence",
                evidence={"event_id": event_id, "category": category, "material_change": False},
            )

    def test_runtime_and_feature_contracts_are_locked(self):
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(runtime_table_names().issubset(tables))
        statuses = dict(self.db.execute(
            "SELECT sport, rule_status FROM multisport_settlement_rules WHERE sportsbook_key='hardrockbet'"
        ).fetchall())
        self.assertEqual(statuses["nba"], "verified")
        self.assertEqual(statuses["epl"], "verified")
        self.assertEqual(statuses["mma"], "verified")
        self.assertEqual(statuses["atp"], "verified")
        self.assertEqual(statuses["wta"], "verified")
        self.assertEqual(set(validate_feature_payload("nba", nba_features())), set(required_feature_names("nba")))
        bad = nba_features(); bad.pop("net_rating_diff")
        with self.assertRaises(ValueError):
            validate_feature_payload("nba", bad)

    def test_history_blocks_future_leakage_and_trains_chronologically(self):
        leaking = {
            "sport": "nba", "provider_sport_key": "basketball_nba",
            "source_event_id": "leak", "event_time": "2026-01-10T00:00:00Z",
            "source_cutoff": "2026-01-10T00:01:00Z", "result_source_key": "official_nba",
            "outcome_role": "home", "features": nba_features(),
        }
        with self.assertRaises(ValueError):
            validate_history_row(leaking)
        rows = []
        start = datetime(2025, 1, 1, tzinfo=UTC)
        for index in range(80):
            event_time = start + timedelta(days=index)
            rows.append({
                "sport": "nba", "provider_sport_key": "basketball_nba",
                "source_event_id": f"hist-{index}", "event_time": event_time.isoformat(),
                "source_cutoff": (event_time - timedelta(hours=1)).isoformat(),
                "result_source_key": "official_nba",
                "outcome_role": "home" if index % 2 == 0 else "away",
                "features": nba_features(index),
            })
        result = import_history_rows(self.db, rows, created_at=NOW)
        self.assertEqual(result["accepted"], 80)
        trained = train_model(
            self.db, sport="nba", feature_version=FEATURE_VERSION,
            training_cutoff=datetime(2025, 4, 1, tzinfo=UTC), min_examples=50, trained_at=NOW,
        )
        self.assertEqual(trained.algorithm, "binary_logistic")
        self.assertGreaterEqual(trained.holdout_count, 10)

    def test_epl_baseline_contract_trains_without_deferred_live_features(self):
        self.assertEqual(
            set(required_feature_names(
                "epl",
                feature_version=EPL_BASELINE_FEATURE_VERSION,
            )),
            set(EPL_BASELINE_FEATURES),
        )
        start = datetime(2025, 1, 1, tzinfo=UTC)
        outcomes = ("home", "draw", "away")
        for index in range(60):
            event_time = start + timedelta(days=index)
            record_training_example(
                self.db,
                sport="epl",
                provider_sport_key="soccer_epl",
                source_event_id=f"epl-baseline-{index}",
                event_time=event_time,
                market_key="three_way_moneyline",
                period="full_game",
                feature_version=EPL_BASELINE_FEATURE_VERSION,
                features=epl_baseline_features(index),
                outcome_role=outcomes[index % len(outcomes)],
                source_cutoff=event_time - timedelta(hours=1),
                result_source_key="football_data_epl_results_archive",
                created_at=NOW,
            )
        trained = train_model(
            self.db,
            sport="epl",
            feature_version=EPL_BASELINE_FEATURE_VERSION,
            training_cutoff=datetime(2025, 4, 1, tzinfo=UTC),
            min_examples=50,
            trained_at=NOW,
        )
        self.assertEqual(trained.algorithm, "softmax")
        self.assertEqual(trained.feature_version, EPL_BASELINE_FEATURE_VERSION)
        self.assertGreaterEqual(trained.holdout_count, 10)

    def test_verified_research_requires_event_specific_evidence(self):
        event_id, _, _ = self.event()
        observation = ResearchObservation(
            sport="nba", event_id=event_id, category="official_status",
            source_key="nba_official", verification_status="verified",
            collected_at=NOW, source_updated_at=NOW,
            source_url="https://official.nba.com/", official=True, payload=None,
        )
        with self.assertRaises(ValueError):
            persist_observation(self.db, observation, as_of=NOW)

    def test_native_export_preserves_verified_book_specific_settlement(self):
        from sports_analytics_pipeline.config import Settings
        from sports_analytics_pipeline.research_export import export_candidates
        self.test_evaluation_is_test_only_before_100()
        self.db.execute("UPDATE multisport_events SET status='scheduled'")
        from dataclasses import replace
        settings = replace(Settings.load(), accessible_bookmakers=("hardrockbet",), market_consensus_bookmakers=("hardrockbet","consensus-a","consensus-b"))
        rows = export_candidates(self.db, settings, as_of=NOW)
        self.assertTrue(rows)
        self.assertTrue(all(q['book'] == 'hardrockbet' or q['settlement_assumed'] for r in rows for q in r['quotes']))
        self.assertTrue(any(q['settlement_assumed'] for r in rows for q in r['quotes']))
        execution = [r for r in rows if r['book_of_record'] == 'hardrockbet']
        public = [r for r in rows if r['book_of_record'] != 'hardrockbet']
        self.assertTrue(execution)
        self.assertTrue(all(r['settlement_rule'] and r['price_scope'] == 'execution' for r in execution))
        self.assertTrue(public)
        self.assertTrue(all(r['price_scope'] == 'public_reference' and not r['book_accessible'] for r in public))
        self.assertTrue(all(not r['settlement_rule'] for r in public))

    def test_four_books_allow_experimental_native_research_before_validation(self):
        self.test_evaluation_is_test_only_before_100()
        event = self.db.execute("SELECT * FROM multisport_events").fetchone()
        self.odds(event['event_id'], event['home_participant_id'], event['away_participant_id'], book="consensus-c", title="Consensus C")
        self.db.execute("DELETE FROM multisport_paper_results")
        self.db.execute("DELETE FROM multisport_paper_decisions")
        self.db.commit()
        result = evaluate_event(self.db, event_id=event['event_id'], prediction_id="pred-test", as_of=NOW,
            accessible_bookmakers=("hardrockbet",), consensus_bookmakers=("hardrockbet","consensus-a","consensus-b","consensus-c"))
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertIn("experimental", result.confidence_label.lower())

    def test_evaluation_is_test_only_before_100(self):
        event_id, home, away = self.event()
        self.odds(event_id, home, away)
        self.odds(event_id, home, away, book="consensus-a", title="Consensus A")
        self.odds(event_id, home, away, book="consensus-b", title="Consensus B")
        self.research(event_id)
        version = self.model()
        record_feature(
            self.db, event_id=event_id, sport="nba", feature_name="market_spread_points",
            feature_value=-3.0, cutoff_time=NOW, feature_version=FEATURE_VERSION,
            collected_at=NOW, source_key="the_odds_api",
        )
        self.db.execute(
            """
            INSERT INTO multisport_predictions (
                prediction_id, event_id, model_version, sport, market_key, period,
                prediction_time, feature_cutoff, probabilities_json,
                uncertainty_margin, status, created_at
            ) VALUES ('pred-test', ?, ?, 'nba', 'spreads', 'full_game', ?, ?, ?, 0.02,
                      'test_only', ?)
            """,
            (event_id, version, NOW.isoformat(), NOW.isoformat(), json.dumps({"home": 0.66, "away": 0.34}), NOW.isoformat()),
        )
        self.db.commit()
        evaluated = evaluate_event(
            self.db, event_id=event_id, prediction_id="pred-test", as_of=NOW,
            accessible_bookmakers=("hardrockbet",),
            consensus_bookmakers=("hardrockbet", "consensus-a", "consensus-b"),
            stale_after_minutes=60,
        )
        self.assertEqual(evaluated.decision_type, "conditional_pick")
        self.assertEqual(evaluated.outcome_role, "home")
        self.assertGreater(evaluated.probability_edge, 0.05)
        self.assertGreaterEqual(evaluated.pick_strength_score, 75)
        self.assertEqual(evaluated.letter_grade, "C")
        self.assertEqual(evaluated.paper_units, 1.0)
        self.assertEqual(evaluated.paper_risk_dollars, 10.0)
        display = status_report(self.db, paper_unit_dollars=25.0, as_of=NOW)[
            "best_current_paper_selection_display"
        ]
        self.assertEqual(display["letter_grade"], "C")
        self.assertEqual(display["paper_risk_dollars"], 25.0)
        self.assertEqual(display["profit_if_win_dollars"], 27.5)
        self.assertEqual(display["total_return_if_win_dollars"], 52.5)
        self.assertFalse(display["score_is_win_probability"])
        decision_id = self.db.execute(
            "SELECT paper_decision_id FROM multisport_paper_decisions WHERE event_id=?",
            (event_id,),
        ).fetchone()[0]
        self.db.execute(
            """
            INSERT INTO multisport_paper_results (
                paper_decision_id, grading_result, settled_at
            ) VALUES (?, 'win', ?)
            """,
            (decision_id, NOW.isoformat()),
        )
        self.db.commit()
        settled = status_report(self.db, paper_unit_dollars=25.0, as_of=NOW)
        self.assertIsNone(settled["best_current_paper_selection_display"])
        self.db.execute(
            "DELETE FROM multisport_paper_results WHERE paper_decision_id=?",
            (decision_id,),
        )
        self.db.commit()
        expired = status_report(
            self.db,
            paper_unit_dollars=25.0,
            as_of=START + timedelta(minutes=1),
        )
        self.assertIsNone(expired["best_current_paper_selection_display"])
        self.assertIsNone(
            expired["sports"]["nba"]["best_current_paper_selection_display"]
        )
        with self.assertRaises(ValueError):
            set_manual_model_approval(
                self.db, sport="nba", model_version=version, status="approved",
                approved_by="human-reviewer", approved_at=NOW,
            )

    def test_consensus_uses_median_and_best_price_does_not_require_primary_book(self):
        event_id, home, away = self.event()
        self.odds(event_id, home, away, 2.50, 1.6666667, "consensus-a", "Consensus A")
        self.odds(event_id, home, away, 2.00, 2.00, "consensus-b", "Consensus B")
        self.odds(event_id, home, away, 1.1111111, 10.00, "consensus-c", "Consensus C")
        self.odds(event_id, home, away, 2.10, 1.80, "bestbook", "Best Book")
        self.verify_settlement("bestbook")
        self.research(event_id)
        version = self.model()
        record_feature(
            self.db, event_id=event_id, sport="nba", feature_name="market_spread_points",
            feature_value=-3.0, cutoff_time=NOW, feature_version=FEATURE_VERSION,
            collected_at=NOW, source_key="the_odds_api",
        )
        self.db.execute(
            """
            INSERT INTO multisport_predictions (
                prediction_id, event_id, model_version, sport, market_key, period,
                prediction_time, feature_cutoff, probabilities_json,
                uncertainty_margin, status, created_at
            ) VALUES ('pred-independent', ?, ?, 'nba', 'spreads', 'full_game', ?, ?, ?,
                      0.02, 'test_only', ?)
            """,
            (
                event_id, version, NOW.isoformat(), NOW.isoformat(),
                json.dumps({"home": 0.66, "away": 0.34}), NOW.isoformat(),
            ),
        )
        self.db.commit()
        result = evaluate_event(
            self.db,
            event_id=event_id,
            prediction_id="pred-independent",
            as_of=NOW,
            accessible_bookmakers=("bestbook",),
            consensus_bookmakers=("consensus-a", "consensus-b", "consensus-c"),
        )
        self.assertEqual(result.decision_type, "conditional_pick")
        self.assertEqual(result.sportsbook_key, "bestbook")
        stored = self.db.execute(
            "SELECT market_consensus_probability FROM multisport_paper_decisions "
            "WHERE paper_decision_id=?",
            (result.paper_decision_id,),
        ).fetchone()
        self.assertAlmostEqual(stored[0], 0.5, places=5)

    def test_two_to_five_point_edge_is_rejected_during_development(self):
        event_id, home, away = self.event()
        self.odds(event_id, home, away)
        self.odds(event_id, home, away, book="consensus-a", title="Consensus A")
        self.odds(event_id, home, away, book="consensus-b", title="Consensus B")
        self.research(event_id)
        version = self.model()
        record_feature(
            self.db, event_id=event_id, sport="nba", feature_name="market_spread_points",
            feature_value=-3.0, cutoff_time=NOW, feature_version=FEATURE_VERSION,
            collected_at=NOW, source_key="the_odds_api",
        )
        self.db.execute(
            """
            INSERT INTO multisport_predictions (
                prediction_id, event_id, model_version, sport, market_key, period,
                prediction_time, feature_cutoff, probabilities_json,
                uncertainty_margin, status, created_at
            ) VALUES ('pred-pass', ?, ?, 'nba', 'spreads', 'full_game', ?, ?, ?, 0.02,
                      'test_only', ?)
            """,
            (
                event_id, version, NOW.isoformat(), NOW.isoformat(),
                json.dumps({"home": 0.52, "away": 0.48}), NOW.isoformat(),
            ),
        )
        self.db.commit()
        result = evaluate_event(
            self.db, event_id=event_id, prediction_id="pred-pass", as_of=NOW,
            accessible_bookmakers=("hardrockbet",),
            consensus_bookmakers=("hardrockbet", "consensus-a", "consensus-b"),
        )
        self.assertEqual(result.decision_type, "pass")
        self.assertIn("5.00 required", result.pass_reason)


    def test_failed_gates_can_be_preserved_as_noncountable_test_observation(self):
        event_id, home, away = self.event("shadow")
        self.odds(event_id, home, away)
        self.odds(event_id, home, away, book="consensus-a", title="Consensus A")
        self.odds(event_id, home, away, book="consensus-b", title="Consensus B")
        version = self.model("nba-shadow-model")
        record_feature(
            self.db, event_id=event_id, sport="nba", feature_name="market_spread_points",
            feature_value=-3.0, cutoff_time=NOW, feature_version=FEATURE_VERSION,
            collected_at=NOW, source_key="the_odds_api",
        )
        self.db.execute(
            """
            INSERT INTO multisport_predictions (
                prediction_id, event_id, model_version, sport, market_key, period,
                prediction_time, feature_cutoff, probabilities_json,
                uncertainty_margin, status, created_at
            ) VALUES ('pred-shadow', ?, ?, 'nba', 'spreads', 'full_game', ?, ?, ?, 0.02,
                      'test_only', ?)
            """,
            (
                event_id, version, NOW.isoformat(), NOW.isoformat(),
                json.dumps({"home": 0.60, "away": 0.40}), NOW.isoformat(),
            ),
        )
        self.db.commit()
        result = evaluate_event(
            self.db, event_id=event_id, prediction_id="pred-shadow", as_of=NOW,
            accessible_bookmakers=("hardrockbet",),
            consensus_bookmakers=("hardrockbet", "consensus-a", "consensus-b"),
            capture_unqualified_test=True,
        )
        self.assertEqual(result.decision_type, "test_only_selection")
        self.assertEqual(result.outcome_role, "home")
        self.assertEqual(result.confidence_label, "Unqualified Test Observation")
        self.assertIn("failed gates", result.pass_reason)
        stored = self.db.execute(
            "SELECT countable_oos, gate_results_json FROM multisport_paper_decisions "
            "WHERE paper_decision_id=?",
            (result.paper_decision_id,),
        ).fetchone()
        self.assertEqual(stored["countable_oos"], 0)
        self.assertFalse(json.loads(stored["gate_results_json"])["research"])

    def test_exposure_blocks_duplicate_participant_and_sport_overload(self):
        first_event, home, first_away = self.event("exposure-1")
        second_event, second_home, second_away = self.event("exposure-2")
        first_odds = self.odds(first_event, home, first_away)
        second_odds = self.odds(second_event, second_home, second_away)
        for index in range(2):
            event_id = first_event if index == 0 else second_event
            participant = home if index == 0 else second_home
            odds_id = first_odds["home"] if index == 0 else second_odds["home"]
            self.db.execute(
                """
                INSERT INTO multisport_paper_decisions (
                    paper_decision_id, event_id, sport, market_key, period,
                    outcome_role, outcome_participant_id, sportsbook_key,
                    odds_snapshot_id, decimal_odds, check_time,
                    decision_type, gate_results_json, manual_approval_status,
                    countable_oos, rule_version, created_at
                ) VALUES (?, ?, 'nba', 'spreads', 'full_game', 'home', ?, 'hardrockbet',
                          ?, 2.0, ?,
                          'test_only_selection', '{}', 'pending', 1, 'test', ?)
                """,
                (
                    f"active-{index}", event_id, participant, odds_id,
                    NOW.isoformat(), NOW.isoformat(),
                ),
            )
        self.db.commit()
        assessment = assess_exposure(
            self.db,
            event_id="nba:basketball_nba:new-event",
            sport="nba",
            outcome_participant_id=home,
            as_of=NOW,
        )
        self.assertFalse(assessment.clear)
        self.assertEqual(assessment.active_for_sport, 2)
        self.assertEqual(assessment.active_for_participant, 1)
        self.assertTrue(any("NBA limit" in blocker for blocker in assessment.blockers))
        self.assertTrue(any("participant" in blocker for blocker in assessment.blockers))

    def test_grade_scale_and_paper_display_are_explicit(self):
        self.assertEqual(letter_grade(100), "A+")
        self.assertEqual(letter_grade(75), "C")
        self.assertEqual(letter_grade(60), "D-")
        self.assertEqual(letter_grade(0), "F-")
        display = decision_presentation(
            {
                "decision_type": "test_only_selection",
                "pick_strength_score": 90,
                "decimal_odds": 2.25,
                "model_probability": 0.60,
                "conservative_probability": 0.57,
                "break_even_probability": 1 / 2.25,
                "probability_edge": 0.1256,
                "ev_per_dollar": 0.2825,
            },
            paper_unit_dollars=20.0,
        )
        self.assertEqual(display["letter_grade"], "A-")
        self.assertEqual(display["american_odds"], 125)
        self.assertEqual(display["paper_risk_dollars"], 20.0)
        self.assertEqual(display["profit_if_win_dollars"], 25.0)

    def test_pass_only_counts_after_event_concludes(self):
        event_id, _, _ = self.event()
        version = self.model()
        self.db.execute(
            """
            INSERT INTO multisport_paper_decisions (
                paper_decision_id, event_id, sport, market_key, period, check_time,
                decision_type, gate_results_json, pass_reason, manual_approval_status,
                countable_oos, model_version, rule_version, created_at
            ) VALUES ('pass-1', ?, 'nba', 'spreads', 'full_game', ?, 'pass', '{}',
                      'Pass - no reliable edge.', 'pending', 1, ?, 'test', ?)
            """,
            (event_id, NOW.isoformat(), version, NOW.isoformat()),
        )
        self.db.commit()
        self.assertEqual(settle_decision(self.db, paper_decision_id="pass-1", settled_at=NOW), "unsettled")
        record_result(
            self.db, event_id=event_id, result_status="final", source_key="official_nba",
            source_updated_at=START + timedelta(hours=3), collected_at=START + timedelta(hours=3),
            home_score=100, away_score=99,
        )
        self.assertEqual(settle_decision(
            self.db, paper_decision_id="pass-1", settled_at=START + timedelta(hours=3)
        ), "pass")
        completed = self.db.execute(
            "SELECT completed_count FROM multisport_validation_metrics WHERE validation_bucket='nba:spreads:full_game'"
        ).fetchone()[0]
        self.assertEqual(completed, 1)

    def test_unknown_parlay_correlation_is_an_automatic_pass(self):
        decision_ids = []
        for index in range(3):
            event_id, home, away = self.event(str(index + 10))
            odds_ids = self.odds(event_id, home, away)
            decision_id = f"leg-{index}"
            self.db.execute(
                """
                INSERT INTO multisport_paper_decisions (
                    paper_decision_id, event_id, sport, market_key, period, outcome_role,
                    outcome_participant_id, sportsbook_key, odds_snapshot_id, points,
                    decimal_odds, check_time, model_probability, decision_type,
                    gate_results_json, manual_approval_status, countable_oos,
                    rule_version, created_at
                ) VALUES (?, ?, 'nba', 'spreads', 'full_game', 'home', ?, 'hardrockbet', ?, -3.0,
                          2.10, ?, 0.60, 'test_only_selection', '{}', 'pending', 1, 'test', ?)
                """,
                (decision_id, event_id, home, odds_ids["home"], NOW.isoformat(), NOW.isoformat()),
            )
            decision_ids.append(decision_id)
        self.db.commit()
        parlay_id = build_three_leg_parlay(
            self.db, paper_decision_ids=tuple(decision_ids), offered_decimal_odds=6.0,
            measured_joint_probability=None, correlation_method=None, created_at=NOW,
        )
        row = self.db.execute("SELECT * FROM multisport_parlays WHERE parlay_id=?", (parlay_id,)).fetchone()
        self.assertEqual(row["decision_type"], "pass")
        self.assertIn("correlation", row["pass_reason"].casefold())

    def test_score_parser_is_limited_to_safe_numeric_team_results(self):
        payload = [{
            "id": "event-1", "sport_key": "basketball_nba",
            "commence_time": "2026-08-17T20:00:00Z", "completed": True,
            "home_team": "Boston Celtics", "away_team": "New York Knicks",
            "scores": [
                {"name": "Boston Celtics", "score": "110"},
                {"name": "New York Knicks", "score": "105"},
            ],
            "last_update": "2026-08-17T21:00:00Z",
        }]
        batch = parse_scores(payload, sport="nba", provider_sport_key="basketball_nba", collected_at=NOW)
        self.assertEqual(batch.scores[0].winner_role, "home")
        with self.assertRaises(ValueError):
            parse_scores(payload, sport="mma", provider_sport_key="mma_mixed_martial_arts", collected_at=NOW)

    def test_status_never_claims_automatic_wagering(self):
        report = status_report(self.db)
        self.assertFalse(report["automatic_wagering"])
        self.assertTrue(report["manual_approval_required"])
        self.assertEqual(report["sports"]["nba"]["completed_oos"], 0)


if __name__ == "__main__":
    unittest.main()

