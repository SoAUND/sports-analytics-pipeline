from datetime import datetime,timedelta,timezone
import copy
import unittest
from sports_analytics_pipeline.supplemental_modeling import train_score_model,score_prediction
from sports_analytics_pipeline.supplemental_review import apply_review

UTC=timezone.utc
NOW=datetime(2026,10,1,tzinfo=UTC)

def games():
    rows=[]
    for i in range(850):
        start=datetime(2025,1,1,tzinfo=UTC)+timedelta(hours=4*i)
        h=i%12;a=(h+1+(i//12)%11)%12
        rows.append({'game_id':str(i),'sport':'cfb','home_name':f'Team {h}','away_name':f'Team {a}',
            'home_score':20+(h*3+i*7)%29,'away_score':15+(a*2+i*11)%31,
            'start_time':start.isoformat(),'final_available_at':(start+timedelta(hours=3)).isoformat(),
            'regular_season':True,'final':True})
    return rows

class ModelTests(unittest.TestCase):
    def test_historical_fit_never_becomes_forward_approval(self):
        model=train_score_model(games(),sport='cfb',as_of=NOW)
        self.assertGreaterEqual(model['historical_holdout_count'],100)
        self.assertEqual(model['oos_count'],0);self.assertFalse(model['approved'])
        self.assertGreater(model['uncertainty'],0)

    def test_model_probabilities_pushes_and_prices_are_independent(self):
        model=train_score_model(games(),sport='cfb',as_of=NOW)
        row={'sport':'cfb','home_name':'Team 1','away_name':'Team 2','market':'spreads','participant':'home','line':-3,'start_time':(NOW+timedelta(days=1)).isoformat(),'decimal_odds':2}
        result=score_prediction(model,row,as_of=NOW)
        self.assertGreaterEqual(result['probability'],0)
        self.assertLessEqual(result['probability']+result['push_probability'],1)
        row['decimal_odds']=3
        self.assertEqual(result,score_prediction(model,row,as_of=NOW))
        row.update(market='totals',participant='under',line=55.5)
        self.assertEqual(score_prediction(model,row,as_of=NOW)['push_probability'],0)

    def test_sparse_exact_line_calibration_blocks_instead_of_capping(self):
        model=train_score_model(games(),sport='cfb',as_of=NOW)
        model['validation_examples']=model['validation_examples'][:5]
        row={'sport':'cfb','home_name':'Team 1','away_name':'Team 2','market':'spreads','participant':'home','line':-3.5,'start_time':(NOW+timedelta(days=1)).isoformat()}
        result=score_prediction(model,row,as_of=NOW)
        self.assertEqual(result['uncertainty'],1)
        self.assertLess(result['local_calibration_count'],20)

    def test_future_unfinal_and_preseason_results_do_not_change_fit(self):
        original=games();model=train_score_model(original,sport='cfb',as_of=NOW)
        extra=copy.deepcopy(original[-1]);extra['game_id']='extra';extra['final_available_at']=(NOW+timedelta(days=1)).isoformat()
        self.assertEqual(model['version'],train_score_model(original+[extra],sport='cfb',as_of=NOW)['version'])
        extra['final_available_at']=original[-1]['final_available_at'];extra['regular_season']=False
        self.assertEqual(model['version'],train_score_model(original+[extra],sport='cfb',as_of=NOW)['version'])

    def test_insufficient_data_or_unknown_team_fails(self):
        with self.assertRaises(ValueError):train_score_model(games()[:50],sport='cfb',as_of=NOW)
        model=train_score_model(games(),sport='cfb',as_of=NOW)
        with self.assertRaises(ValueError):score_prediction(model,{'sport':'cfb','start_time':(NOW+timedelta(days=1)).isoformat(),'home_name':'Unknown','away_name':'Team 1'},as_of=NOW)

    def test_conflicting_game_data_is_rejected(self):
        rows=games();extra=copy.deepcopy(rows[0]);extra['home_score']+=1
        with self.assertRaises(ValueError):train_score_model(rows+[extra],sport='cfb',as_of=NOW)

    def test_review_requires_event_and_book_specific_rules(self):
        row={'event_id':'e','sport':'nhl','home_name':'Home','away_name':'Away','start_time':(NOW+timedelta(days=1)).isoformat(),
             'market':'moneyline','period':'full_game','book_of_record':'hardrockbet_fl','settlement_rule':'','quotes':[{'book':'hardrockbet_fl'},{'book':'draftkings'}]}
        record={'event_id':'e','reviewed':True,'reviewer':'reviewer','reviewed_at':NOW.isoformat(),'home_name':'Home','away_name':'Away',
            'start_time':row['start_time'],'material_information_complete':True,'model_information_accounted_for':True,'evidence':[]}
        rule={'sport':'nhl','market':'moneyline','period':'full_game','book':'hardrockbet_fl','state':'fl','rule_key':'ot-so',
              'reviewed':True,'reviewer':'reviewer','reviewed_at':NOW.isoformat(),'source_url':'https://www.hardrock.bet/fl/house-rules/'}
        result=apply_review(row,{'events':[record],'settlement_rules':[rule]},as_of=NOW,accessible_books=('hardrockbet_fl',))
        self.assertEqual(result['settlement_rule'],'ot-so');self.assertEqual(len(result['quotes']),1)
        rule['state']='in'
        self.assertEqual(apply_review(row,{'events':[record],'settlement_rules':[rule]},as_of=NOW,accessible_books=('hardrockbet_fl',))['settlement_rule'],'')

    def test_unapproved_source_cannot_claim_official_verification(self):
        row={'event_id':'e','sport':'cfb','home_name':'Home','away_name':'Away','start_time':NOW.isoformat(),'market':'spreads','period':'full_game','book_of_record':'hardrockbet_fl','quotes':[]}
        record={**row,'reviewed':True,'reviewer':'reviewer','reviewed_at':NOW.isoformat(),'evidence':[{'source_key':'fake','source_url':'https://fake.test','official':True,'facts':{'lineup':'confirmed'},'source_updated_at':NOW.isoformat(),'collected_at':NOW.isoformat()}]}
        self.assertEqual(apply_review(row,{'events':[record]},as_of=NOW,accessible_books=())['evidence'],[])
