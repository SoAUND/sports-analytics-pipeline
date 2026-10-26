from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.mlb_status_feed import (
    build_gate_payload,
    build_result_payload,
    hypothetical_payouts,
    render_status_comment,
)


class MLBStatusFeedTests(unittest.TestCase):
    def test_gate_payload_is_structured_test_only_status(self):
        payload = build_gate_payload(
            workflow_run_id="123",
            workflow_run_attempt="2",
            operation_date="2026-08-17",
            automated="true",
            run_mode="paper_run",
            checked_at="2026-08-18T01:00:00+00:00",
        )
        self.assertEqual(payload["event_type"], "paper_run_gate_due")
        self.assertTrue(payload["automated"])
        self.assertEqual(payload["mode"], "test_only")
        self.assertFalse(payload["automatic_wagering"])
        comment = render_status_comment(payload)
        self.assertIn("our-mlb-status-feed:v1", comment)
        self.assertIn('"workflow_run_id": "123"', comment)

    def test_hypothetical_payouts_are_profit_and_total_return_examples(self):
        payouts = hypothetical_payouts(1.8)
        self.assertEqual(payouts["$2"]["profit_if_win"], 1.6)
        self.assertEqual(payouts["$2"]["total_return_if_win"], 3.6)
        self.assertEqual(payouts["$5"]["profit_if_win"], 4.0)
        self.assertEqual(payouts["$5"]["total_return_if_win"], 9.0)

    def test_missing_database_reports_failure_without_inventing_results(self):
        payload = build_result_payload(
            database_path="/definitely/missing/mlb.sqlite3",
            workflow_run_id="500",
            workflow_run_attempt=1,
            operation_date="2026-08-17",
            automated=True,
            checked_at="2026-08-18T01:00:00+00:00",
            job_status="failure",
            canonical_outcome="skipped",
        )
        self.assertEqual(payload["event_type"], "paper_run_failed")
        self.assertFalse(payload["database_available"])
        self.assertFalse(payload["canonical_database_saved"])
        self.assertEqual(payload["run_decisions"]["new_decisions"], 0)
        self.assertFalse(payload["automatic_wagering"])

    def test_current_operation_selection_and_progress_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            db = connect(db_path)
            initialize_schema(db)
            with db:
                db.execute(
                    """
                    INSERT INTO stadiums(
                        stadium_id, official_name, city, state_or_region, country_code,
                        latitude, longitude, time_zone, roof_type, surface_type,
                        weather_exposed, valid_from_season, valid_through_season,
                        verified_on, source_url
                    ) VALUES ('park','Test Park','Miami','FL','US',25.0,-80.0,
                              'America/New_York','open','natural_grass',1,2026,NULL,
                              '2026-01-01','https://example.com/park')
                    """
                )
                db.execute(
                    """
                    INSERT INTO teams(
                        team_id,sport,official_name,city_name,nickname,abbreviation,
                        conference,division,primary_stadium_id,active_from_season,
                        active_through_season,verified_on
                    ) VALUES ('away','mlb','Away Club','Away City','Away','AWY','AL','East',
                              'park',2026,NULL,'2026-01-01')
                    """
                )
                db.execute(
                    """
                    INSERT INTO teams(
                        team_id,sport,official_name,city_name,nickname,abbreviation,
                        conference,division,primary_stadium_id,active_from_season,
                        active_through_season,verified_on
                    ) VALUES ('home','mlb','Home Club','Home City','Home','HOM','NL','East',
                              'park',2026,NULL,'2026-01-01')
                    """
                )
                db.execute(
                    """
                    INSERT INTO games(
                        game_id,source_game_id,sport,season,season_type,week,
                        home_team_id,away_team_id,kickoff_time,stadium_id,status,
                        created_at,updated_at
                    ) VALUES ('g1','source-1','mlb',2026,'regular',0,'home','away',
                              '2026-08-18T01:40:00+00:00','park','scheduled',
                              '2026-08-18T00:00:00+00:00','2026-08-18T00:00:00+00:00')
                    """
                )
                db.execute(
                    """
                    INSERT INTO odds_snapshots(
                        game_id,sportsbook_key,market_key,period,settlement_rule,
                        outcome_team_id,opponent_team_id,spread_points,decimal_odds,
                        american_odds,source_updated_at,collected_at,market_status,
                        is_accessible,is_consensus_eligible
                    ) VALUES ('g1','hardrockbet_fl','moneylines','full_game','winner',
                              'home','away',NULL,1.8,-125,
                              '2026-08-18T01:05:00+00:00','2026-08-18T01:05:10+00:00',
                              'open',1,1)
                    """
                )
                selected_snapshot = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                db.execute(
                    """
                    INSERT INTO odds_snapshots(
                        game_id,sportsbook_key,market_key,period,settlement_rule,
                        outcome_team_id,opponent_team_id,spread_points,decimal_odds,
                        american_odds,source_updated_at,collected_at,market_status,
                        is_accessible,is_consensus_eligible
                    ) VALUES ('g1','hardrockbet_fl','moneylines','full_game','winner',
                              'away','home',NULL,2.1,110,
                              '2026-08-18T01:05:00+00:00','2026-08-18T01:05:10+00:00',
                              'open',1,1)
                    """
                )
                db.execute(
                    """
                    INSERT INTO mlb_operations_runs(
                        run_id,operation_date,mode,status,started_at,completed_at,
                        failed_step,summary_json
                    ) VALUES ('op1','2026-08-17','test_only','succeeded',
                              '2026-08-18T01:01:00+00:00','2026-08-18T01:10:00+00:00',
                              NULL,'{}')
                    """
                )
                db.execute(
                    """
                    INSERT INTO model_runs(
                        model_run_id,game_id,sport,market_key,model_version,
                        feature_version,data_version,rule_version,training_cutoff,
                        prediction_time,home_cover_probability,away_cover_probability,
                        push_probability,uncertainty_margin,market_spread_points,
                        oos_decision_count,calibration_status,clv_status,status,created_at
                    ) VALUES ('mr1','g1','mlb','moneylines','mlb-model-v1',
                              'mlb-feature-v1','data-v1','test-rule',
                              '2026-08-17T00:00:00+00:00','2026-08-18T01:06:00+00:00',
                              0.61,0.39,0,0.02,NULL,0,'acceptable','unknown',
                              'test_only','2026-08-18T01:06:00+00:00')
                    """
                )
                live_report = {
                    "games": [{"game_id": "g1", "kickoff_time": "2026-08-18T01:40:00+00:00", "status": "ready", "blockers": []}],
                    "test_only_selections": 1,
                    "passes": 0,
                }
                db.execute(
                    """
                    INSERT INTO mlb_live_paper_runs(
                        live_run_id,operation_date,operations_run_id,mode,status,
                        scheduled_games,ready_games,blocked_games,paper_decisions,
                        report_json,recorded_at
                    ) VALUES ('live1','2026-08-17','op1','test_only','ready',1,1,0,1,?,
                              '2026-08-18T01:08:00+00:00')
                    """,
                    (json.dumps(live_report),),
                )
                db.execute(
                    """
                    INSERT INTO paper_decisions(
                        paper_decision_id,game_id,season_type,market_key,period,
                        decision_type,selected_team_id,sportsbook_key,
                        selected_odds_snapshot_id,decimal_odds,model_probability,
                        conservative_probability,break_even_probability,
                        probability_edge,ev_per_dollar,minimum_acceptable_decimal_odds,
                        pick_strength_score,confidence_label,rule_version,gate_results_json,reasons_json,
                        loss_reasons_json,source_notes_json,countable_oos,decision_time,
                        created_at
                    ) VALUES ('pd1','g1','regular','moneylines','full_game',
                              'test_only_selection','home','hardrockbet_fl',?,1.8,0.61,0.59,
                              0.555555,0.034445,0.062,1.85,88,'highest_confidence',
                              'test-rule','{}','[]','[]','[]',1,
                              '2026-08-18T01:07:00+00:00','2026-08-18T01:07:00+00:00')
                    """,
                    (selected_snapshot,),
                )
            db.close()

            payload = build_result_payload(
                database_path=db_path,
                workflow_run_id="900",
                workflow_run_attempt=1,
                operation_date="2026-08-17",
                automated=True,
                checked_at="2026-08-18T01:00:00+00:00",
                job_status="success",
                canonical_outcome="success",
            )

        self.assertEqual(payload["event_type"], "paper_run_completed")
        self.assertTrue(payload["canonical_database_saved"])
        self.assertEqual(payload["live_readiness"]["ready_games"], 1)
        self.assertEqual(payload["run_decisions"]["test_only_selections"], 1)
        selection = payload["run_decisions"]["selections"][0]
        self.assertEqual(selection["matchup"], "Away Club @ Home Club")
        self.assertEqual(selection["american_odds"], -125)
        self.assertEqual(selection["pick_strength_grade"], "A-")
        self.assertEqual(selection["pick_strength_display"], "88/100 (Grade A-)")
        self.assertEqual(payload["run_decisions"]["evaluations"][0]["pick_strength_grade"], "A-")
        self.assertAlmostEqual(selection["market_no_vig_probability"], 0.538462, places=6)
        self.assertEqual(selection["hypothetical_payouts"]["$5"]["total_return_if_win"], 9.0)
        self.assertEqual(selection["paper_unit_dollars"], 5.0)
        self.assertIsNone(selection["paper_profit_dollars"])
        self.assertEqual(payload["model_screening"]["model_runs"], 1)
        screen = payload["model_screening"]["screens"][0]
        self.assertEqual(screen["matchup"], "Away Club @ Home Club")
        self.assertFalse(screen["accessible_price_verification_required"])
        self.assertEqual(screen["accessible_bookmakers"], ["hardrockbet_fl"])
        self.assertEqual(screen["status"], "screen_only_not_a_selection")
        self.assertEqual(screen["sides"][0]["indicative_american_odds"], -125)
        self.assertFalse(screen["sides"][0]["meets_five_point_edge_indicatively"])
        self.assertEqual(payload["progress"]["recorded_regular_season_decisions"], 1)
        self.assertEqual(payload["progress"]["paper_profit_units"], 0.0)
        self.assertEqual(payload["progress"]["paper_profit_dollars"], 0.0)
        self.assertFalse(payload["automatic_wagering"])

    def test_workflow_posts_known_issue_feed_without_weakening_test_only_gate(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / ".github/workflows/mlb-paper.yml").read_text(encoding="utf-8")
        self.assertIn("issues: write", source)
        self.assertIn("gh issue comment 21", source)
        self.assertIn("run_mlb_status_feed.py gate", source)
        self.assertIn("run_mlb_status_feed.py result", source)
        self.assertIn("CANONICAL_OUTCOME", source)
        self.assertIn('if settings.bot_mode not in {"research", "test_only"}:', source)
        self.assertIn("BOT_MODE must remain research or test_only", source)
        self.assertIn("if: always() && needs.gate.outputs.run_mode == 'paper_run'", source)


if __name__ == "__main__":
    unittest.main()

