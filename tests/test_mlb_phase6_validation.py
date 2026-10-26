from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json
import unittest

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.data.seed import seed_reference_data
from sports_analytics_pipeline.mlb_validation import build_mlb_validation_report, store_mlb_validation_review
from sports_analytics_pipeline.paper import record_paper_decision

UTC = timezone.utc
NOW = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)


class MLBPhase6ValidationTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        initialize_schema(self.db)
        seed_reference_data(self.db, include_mlb=True)

    def tearDown(self):
        self.db.close()

    def _game(self, i: int, *, sport="mlb"):
        home, away = (("mlb_nyy", "mlb_bos") if sport == "mlb" else ("BUF", "MIA"))
        stadium = "yankee_stadium" if sport == "mlb" else self.db.execute(
            "SELECT primary_stadium_id FROM teams WHERE team_id=?", (home,)
        ).fetchone()[0]
        gid=f"{sport}_v_{i}"
        kickoff=NOW+timedelta(hours=5, minutes=i)
        self.db.execute(
            """INSERT INTO games(game_id,source_game_id,sport,season,season_type,week,home_team_id,away_team_id,
            kickoff_time,stadium_id,status,home_score,away_score,created_at,updated_at)
            VALUES(?,?,?,?, 'regular',0,?,?,?,?, 'final',?,?,?,?)""",
            (gid,gid,sport,2026,home,away,kickoff.isoformat(),stadium,5 if i%2==0 else 2,2 if i%2==0 else 5,NOW.isoformat(),NOW.isoformat())
        )
        self.db.commit()
        return self.db.execute("SELECT * FROM games WHERE game_id=?",(gid,)).fetchone()

    def _selection(self, game, i: int, *, probability=0.60, edge=0.06, confidence="strong"):
        team=game["home_team_id"]
        opp=game["away_team_id"]
        odds=1.90
        cur=self.db.execute(
            """INSERT INTO odds_snapshots(game_id,sportsbook_key,market_key,period,settlement_rule,outcome_team_id,
            opponent_team_id,spread_points,decimal_odds,source_updated_at,collected_at,market_status,is_accessible,is_consensus_eligible)
            VALUES(?, 'hardrockbet','moneylines','full_game','standard',?,?,NULL,?,?,?,'open',1,1)""",
            (game["game_id"],team,opp,odds,NOW.isoformat(),NOW.isoformat())
        )
        oid=cur.lastrowid
        outcome={
            "market_key":"moneylines","decision":"test_only_selection","team_id":team,
            "sportsbook":"hardrockbet","odds_snapshot_id":oid,"decimal_odds":odds,"checked_at":NOW.isoformat(),
            "model_probability":probability,"conservative_probability":probability-0.02,"push_probability":0.0,
            "break_even_probability":1/odds,"probability_edge":edge,"ev_per_dollar":0.08,
            "minimum_acceptable_decimal_odds":1.80,"pick_strength_score":80,"confidence":confidence,
            "model_version":"mlb-elo-logit-v1","feature_version":"mlb-elo-v1","data_version":"phase6-test",
            "gates":{},"supporting_reasons":[],"edge_loss_reasons":[],"source_notes":[],"pass_reason":None,
        }
        result=record_paper_decision(self.db,game=game,outcome=outcome,decision_time=NOW+timedelta(seconds=i))
        pid=result["paper_decision_id"]
        win=i%2==0
        self.db.execute(
            """INSERT INTO paper_results(paper_decision_id,home_score,away_score,cover_result,push_status,
            entry_spread,entry_decimal_odds,closing_spread,closing_decimal_odds,closing_sportsbook_key,
            closing_snapshot_time,consensus_closing_probability,line_clv_points,exact_line_price_clv_probability,
            close_comparison,paper_profit_loss,settled_at)
            VALUES(?,?,?, ?,0,NULL,?,NULL,?,'hardrockbet',?,NULL,NULL,?,?,?,?)""",
            (pid,5 if win else 2,2 if win else 5,"win" if win else "loss",odds,1.85,
             (NOW+timedelta(hours=4)).isoformat(),0.01 if win else -0.01,"better" if win else "worse",0.9 if win else -1.0,
             (NOW+timedelta(hours=6)).isoformat())
        )
        self.db.commit()

    def _pass(self, game, i: int):
        outcome={"market_key":"moneylines","decision":"pass","model_probability":0.5,"conservative_probability":0.48,
                 "push_probability":0.0,"model_version":"mlb-elo-logit-v1","feature_version":"mlb-elo-v1",
                 "data_version":"phase6-test","gates":{},"supporting_reasons":[],"edge_loss_reasons":[],
                 "source_notes":[],"pass_reason":"missing confirmed lineup"}
        record_paper_decision(self.db,game=game,outcome=outcome,decision_time=NOW+timedelta(seconds=i))

    def test_below_100_is_insufficient_sample_and_never_approved(self):
        for i in range(99): self._pass(self._game(i),i)
        report=build_mlb_validation_report(self.db)
        self.assertEqual(report["review_status"],"insufficient_sample")
        self.assertFalse(report["automatic_approval"])
        self.assertEqual(report["remaining_to_review_threshold"],1)

    def test_100_triggers_review_required_not_approval(self):
        for i in range(100): self._pass(self._game(i),i)
        report=build_mlb_validation_report(self.db)
        self.assertEqual(report["review_status"],"review_required")
        self.assertFalse(report["automatic_approval"])
        self.assertIn("does not validate",report["notice"])

    def test_nfl_decisions_never_count_toward_mlb_review(self):
        for i in range(100): self._pass(self._game(i,sport="nfl"),i)
        self.assertEqual(build_mlb_validation_report(self.db)["recorded_oos_decisions"],0)

    def test_report_calculates_record_roi_drawdown_and_streak(self):
        for i in range(6): self._selection(self._game(i),i)
        r=build_mlb_validation_report(self.db,target=1)
        self.assertEqual(r["record"],{"wins":3,"losses":3,"pushes":0})
        self.assertAlmostEqual(r["profit_units"],-0.3)
        self.assertEqual(r["paper_unit_dollars"],5.0)
        self.assertEqual(r["profit_dollars"],-1.5)
        self.assertEqual(r["longest_losing_streak"],1)
        self.assertGreaterEqual(r["maximum_drawdown_units"],1.0)
        self.assertGreaterEqual(r["maximum_drawdown_dollars"],5.0)

    def test_edge_band_performance_is_reported(self):
        self._selection(self._game(1),1,edge=0.055)
        self._selection(self._game(2),2,edge=0.085)
        bands={x["band"] for x in build_mlb_validation_report(self.db,target=1)["edge_band_performance"]}
        self.assertEqual(bands,{"5_to_7_5pct","7_5_to_10pct"})

    def test_confidence_performance_is_reported(self):
        self._selection(self._game(1),1,confidence="strong")
        self._selection(self._game(2),2,confidence="moderate")
        bands={x["band"] for x in build_mlb_validation_report(self.db,target=1)["confidence_performance"]}
        self.assertEqual(bands,{"moderate","strong"})

    def test_calibration_and_clv_evidence_are_reported(self):
        for i in range(4): self._selection(self._game(i),i,probability=0.62)
        r=build_mlb_validation_report(self.db,target=1)
        self.assertEqual(r["calibration"]["scored_selections"],4)
        self.assertIsNotNone(r["calibration"]["brier_score"])
        self.assertEqual(r["clv"]["exact_price_samples"],4)
        self.assertEqual(r["clv"]["better"],2)
        self.assertEqual(r["clv"]["worse"],2)

    def test_pass_reasons_are_audited(self):
        for i in range(3): self._pass(self._game(i),i)
        r=build_mlb_validation_report(self.db,target=1)
        self.assertEqual(r["top_pass_reasons"][0]["count"],3)

    def test_review_snapshot_is_immutable_test_only_evidence(self):
        self._pass(self._game(1),1)
        report=build_mlb_validation_report(self.db,target=1)
        rid=store_mlb_validation_review(self.db,report=report,reviewed_at=NOW)
        row=self.db.execute("SELECT * FROM mlb_validation_reviews WHERE review_id=?",(rid,)).fetchone()
        self.assertEqual(row["automatic_approval"],0)
        self.assertEqual(json.loads(row["report_json"])["mode"],"test_only")

    def test_review_snapshot_requires_timezone(self):
        report=build_mlb_validation_report(self.db,target=1)
        with self.assertRaisesRegex(ValueError,"time zone"):
            store_mlb_validation_review(self.db,report=report,reviewed_at=datetime(2026,8,16,12,0))

    def test_invalid_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"positive"):
            build_mlb_validation_report(self.db,target=0)


if __name__ == "__main__": unittest.main()
