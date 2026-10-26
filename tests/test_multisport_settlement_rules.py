import json
import unittest
from datetime import datetime, timezone
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap
from sports_analytics_pipeline.multisport_results import _grade_moneyline

NOW=datetime(2026,8,29,19,30,tzinfo=timezone.utc)
class SettlementRulesTests(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:'); initialize_schema(self.db); bootstrap(self.db,as_of=NOW)
    def tearDown(self): self.db.close()
    def test_seeded_verified_rules(self):
        rows=self.db.execute("SELECT sportsbook_key,sport,rule_status FROM multisport_settlement_rules WHERE (sportsbook_key='hardrockbet' AND sport IN ('mma','atp','wta')) OR (sportsbook_key='draftkings' AND sport IN ('atp','wta'))").fetchall()
        self.assertEqual(len(rows),5); self.assertTrue(all(r['rule_status']=='verified' for r in rows))
    def test_book_rule_mma(self):
        rule={'rule_status':'verified','rule_key':'book_mma_two_way_draw_push_no_contest_void'}; d={'sport':'mma','outcome_role':'home'}
        self.assertEqual(_grade_moneyline(d,{'result_status':'final','winner_role':'draw','raw_result_json':'{}'},rule),'push')
        self.assertEqual(_grade_moneyline(d,{'result_status':'no_contest','winner_role':'void','raw_result_json':'{}'},rule),'void')
    def test_book_rule_tennis(self):
        rule={'rule_status':'verified','rule_key':'book_tennis_first_set_retirement_action'}; d={'sport':'atp','outcome_role':'home'}
        self.assertEqual(_grade_moneyline(d,{'result_status':'retirement','winner_role':'home','raw_result_json':'{}'},rule),'unsettled')
        self.assertEqual(_grade_moneyline(d,{'result_status':'retirement','winner_role':'away','raw_result_json':json.dumps({'completed_sets':0})},rule),'void')
        self.assertEqual(_grade_moneyline(d,{'result_status':'retirement','winner_role':'home','raw_result_json':json.dumps({'completed_sets':1})},rule),'win')
    def test_draftkings_tennis(self):
        rule={'rule_status':'verified','rule_key':'draftkings_tennis_match_moneyline_interrupted_advancer'}; result={'result_status':'retirement','winner_role':'home','raw_result_json':json.dumps({'completed_points':3})}
        self.assertEqual(_grade_moneyline({'sport':'wta','outcome_role':'home'},result,rule),'win')
        self.assertEqual(_grade_moneyline({'sport':'wta','outcome_role':'away'},result,rule),'void')
if __name__=='__main__': unittest.main()
