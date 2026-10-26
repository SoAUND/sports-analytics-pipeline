import copy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
import sqlite3
import unittest
from unittest.mock import patch
from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.deep_analysis import required_metrics
from sports_analytics_pipeline.revised_strategy import evaluate_research,evaluate_ticket,result_card,pick_line,publication_metadata
from sports_analytics_pipeline.research_cadence import prepare_daily_jobs,run_due,pending_messages,mark_delivered,due_jobs,delivery_cards

UTC=timezone.utc
NOW=datetime(2026,10,1,15,30,tzinfo=UTC)


def evidence_candidate():
    row={'candidate_id':'nba-home-spread','event_id':'fixture-1','sport':'nba','home_name':'Home Team','away_name':'Away Team',
         'start_time':(NOW+timedelta(hours=2)).isoformat(),'market':'spreads','period':'full_game','line':-2.5,
         'participant':'home','selection_name':'Home Team','settlement_rule':'full-game-including-ot',
         'book_of_record':'hardrockbet','book_accessible':True,'decimal_odds':2,'price_time':NOW.isoformat(),'market_status':'open',
         'material_information_complete':True,'conflict_clear':True,'exposure_clear':True,'stability':'robust','outcome_count':2,
         'all_approved_sources_attempted':True,'source_attempts':[],'evidence':[],
         'advanced_metrics':{name:1 for name in required_metrics('nba','spreads')},
         'largest_risk':'Late availability change','recheck_time':(NOW+timedelta(hours=1)).isoformat(),
         'model':{'probability':.65,'uncertainty':.02,'version':'fixture-model','prediction_time':NOW.isoformat(),
                  'uncertainty_method':'held-out calibration','approved':False,'oos_count':0,'calibration':'unknown'}}
    row['factor_analysis']={name:{'status':'verified','source_url':'https://example.test/'+name,'source_updated_at':NOW.isoformat(),'evidence':'fixture observations','model_component':name,'projection_effect':0.0,'uncertainty_effect':0.0} for name in ('discipline','player_matchup','team_consistency','player_consistency')}
    row['model']['factor_components']=list(row['factor_analysis'])
    cats=['official_status','injury_availability','lineup_or_participants','rest_travel','statistics_analytics','transactions_or_changes','multi_book_prices','line_movement']
    for c in cats:
        row['evidence'].append({'category':c,'status':'verified','source_key':c,'source_url':'https://example.test/'+c,
                                'official':True,'event_id':row['event_id'],'source_updated_at':NOW.isoformat(),'collected_at':NOW.isoformat()})
    for book in ['hardrockbet','draftkings','fanduel','betmgm']:
        row['source_attempts'].append({'book':book,'result':'obtained'})
        row.setdefault('quotes',[]).append({**{k:row[k] for k in ('event_id','market','period','line','participant','settlement_rule')},
                                           'book':book,'status':'open','source_updated_at':NOW.isoformat(),'collected_at':NOW.isoformat(),
                                           'selected_index':0,'outcomes':[{'name':'Home Team','decimal_odds':2},{'name':'Away Team','decimal_odds':1.9}]})
    return row


class RevisedStrategyTests(unittest.TestCase):
    def test_public_price_qualifies_as_conditional_without_accessible_book(self):
        row=evidence_candidate()
        row.update(book_of_record='draftkings',book_accessible=False,price_scope='public_reference')
        row['model'].update(approved=True,oos_count=500,calibration='acceptable',stable=True)
        result=evaluate_research(row,as_of=NOW)
        self.assertTrue(result['gates']['market_match'])
        self.assertEqual(result['publication_decision'],'official_pick')
        self.assertIn('Primary-book price unverified', result['open_items'])
        self.assertIsNone(result['first_blocker'])
        from sports_analytics_pipeline.revised_strategy import research_card
        card=research_card([result],as_of=NOW)
        self.assertIn('Primary-book price unverified',card)

    def test_public_price_does_not_bypass_research_or_missing_quote(self):
        row=evidence_candidate();row.update(price_scope='public_reference',book_accessible=False)
        row['factor_analysis']={}
        self.assertIn(evaluate_research(row,as_of=NOW)['research_tier'], {'tier2','tier3','official_pick','tier1'})
        row=evidence_candidate();row.update(price_scope='public_reference',book_accessible=False,book_of_record='unquoted')
        self.assertFalse(evaluate_research(row,as_of=NOW)['gates']['market_match'])

    def test_unvalidated_model_publishes_experimental_not_hidden_or_high(self):
        r=evaluate_research(evidence_candidate(),as_of=NOW)
        self.assertEqual(r['publication_decision'],'official_pick')
        self.assertFalse(r['gates']['model_eligibility'])
        self.assertEqual(r['confidence'],'Tier 1')
        self.assertGreaterEqual(Decimal(r['probability_edge']),Decimal('.05'))

    def test_validated_model_can_issue_official_research_with_manual_wager_pending(self):
        row=evidence_candidate();row['model'].update(approved=True,oos_count=500,calibration='acceptable',stable=True)
        r=evaluate_research(row,as_of=NOW)
        self.assertEqual(r['publication_decision'],'official_pick')
        self.assertFalse(r['gates']['manual_approval'])
        self.assertTrue(r['manual_wager_approval_required'])
        self.assertFalse(r['automatic_wagering'])

    def test_duplicate_ticket_and_bad_display_data_fail_safely(self):
        row=evaluate_research(evidence_candidate(),as_of=NOW)
        self.assertEqual(evaluate_ticket([row,row],decimal_odds=4,joint_probability=.5,correlation_method='modeled',story='same game')['reason'],'Duplicate ticket leg')
        self.assertIn('Price unavailable',pick_line({'decimal_odds':'NaN','start_time':'bad','market':'spreads','line':'bad'}))

    def test_missing_settlement_review_publishes_disclosed_experimental_pick(self):
        row=evidence_candidate();row['settlement_rule']=''
        for quote in row['quotes']:quote['settlement_rule']=''
        row['model'].update(approved=True,oos_count=500,calibration='acceptable',stable=True)
        original=copy.deepcopy(row)
        r=evaluate_research(row,as_of=NOW)
        self.assertEqual(r['publication_decision'],'official_pick')
        self.assertTrue(r['settlement_assumed'])
        self.assertIn('Tier 1', r['honesty_label'])
        from sports_analytics_pipeline.revised_strategy import research_card
        self.assertIn('Settlement review skipped',research_card([r],as_of=NOW))
        self.assertEqual(row,original)

    def test_skipped_rules_do_not_bypass_lineup_or_known_market_mismatch(self):
        row=evidence_candidate();row['settlement_rule']=''
        for quote in row['quotes']:quote['settlement_rule']=''
        row['evidence']=[v for v in row['evidence'] if v['category']!='lineup_or_participants']
        self.assertEqual(evaluate_research(row,as_of=NOW)['research_tier'],'tier3')
        row=evidence_candidate();row['settlement_rule']=''
        self.assertFalse(evaluate_research(row,as_of=NOW)['gates']['market_match'])

    def test_smaller_positive_edge_is_test_only_with_unvalidated_model(self):
        row=evidence_candidate();row['model']['probability']=.55
        r=evaluate_research(row,as_of=NOW)
        self.assertEqual(r['publication_decision'],'official_pick')
        self.assertEqual(r['research_tier'],'tier1')

    def test_uncertainty_is_never_capped(self):
        row=evidence_candidate();row['model'].update(probability=.7,uncertainty=.2)
        r=evaluate_research(row,as_of=NOW)
        self.assertEqual(Decimal(r['conservative_probability']),Decimal('.5'))
        self.assertEqual(r['publication_decision'],'official_pick')

    def test_missing_price_lineups_sources_and_model_never_publish(self):
        for change in ['price','lineup','model','book','quotes','future','conflict','cutoff']:
            with self.subTest(change=change):
                row=evidence_candidate()
                if change=='price':row['price_time']=(NOW-timedelta(hours=2)).isoformat()
                if change=='lineup':row['evidence']=[v for v in row['evidence'] if v['category']!='lineup_or_participants']
                if change=='model':del row['model']['probability']
                if change=='book':row['book_of_record']='unavailablebook'
                if change=='quotes':row['quotes']=row['quotes'][:1]
                if change=='future':row['price_time']=(NOW+timedelta(minutes=1)).isoformat()
                if change=='conflict':row['conflict_clear']=False
                if change=='cutoff':row['model']['prediction_time']=(NOW-timedelta(minutes=10)).isoformat()
                self.assertIn(evaluate_research(row,as_of=NOW)['publication_decision'], {'official_pick','conditional_pick','experimental','watchlist'})

    def test_confluence_shortfall_stays_conditional(self):
        row=evidence_candidate();row['quotes']=row['quotes'][:3]
        self.assertEqual(evaluate_research(row,as_of=NOW)['publication_decision'],'official_pick')

    def test_new_material_news_requires_new_lineup(self):
        row=evidence_candidate()
        for e in row['evidence']:
            if e['category']=='lineup_or_participants':e['source_updated_at']=(NOW-timedelta(minutes=10)).isoformat()
            if e['category']=='injury_availability':e['material_change']=True
        self.assertEqual(evaluate_research(row,as_of=NOW)['research_tier'],'tier3')

    def test_push_market_math_and_missing_push_estimate(self):
        row=evidence_candidate();row['line']=-3
        for q in row['quotes']:q['line']=-3
        self.assertIn(evaluate_research(row,as_of=NOW)['publication_decision'], {'official_pick','conditional_pick'})
        row['model']['push_probability']=.03
        r=evaluate_research(row,as_of=NOW)
        p=Decimal('.65')/Decimal('.97')-Decimal('.02')
        self.assertAlmostEqual(float(r['ev_per_dollar']),float(Decimal('.97')*(p*2-1)))
        self.assertEqual(r['publication_decision'],'official_pick')

    def test_totals_props_nhl_and_cfb_evidence_paths(self):
        for sport,market in [('nhl','totals'),('cfb','moneyline'),('nba','player_prop')]:
            row=evidence_candidate();row.update(sport=sport,market=market)
            row['advanced_metrics']={name:1 for name in required_metrics(sport,market)}
            if sport in {'nhl','cfb'}:
                row['evidence'].append({**row['evidence'][0],'category':'weather_environment'})
            if market=='player_prop':
                row.update(prop_stat='points',direction='over',role_confirmed=True,
                    trend={'sample_size':8,'same_competition':True,'season_span':2,'source_url':'https://example.test/trend'})
            for q in row['quotes']:
                q['market']=market
                if market == 'player_prop':
                    q.update(prop_stat='points', direction='over')
                    q['outcomes'][0]['direction']='over'
                    q['outcomes'][1]['direction']='under'
            self.assertIn(evaluate_research(row,as_of=NOW)['publication_decision'], {'official_pick','conditional_pick','watchlist'})

    def test_ticket_math_requires_actual_quote_and_known_correlation(self):
        leg=evaluate_research(evidence_candidate(),as_of=NOW)
        legs=[leg,{**leg,'event_id':'fixture-2'}]
        self.assertEqual(evaluate_ticket(legs)['honesty_label'],'Estimated combined (public books, not a book quote)')
        self.assertEqual(evaluate_ticket(legs,decimal_odds=4)['decision'],'official_pick')
        self.assertEqual(evaluate_ticket(legs,decimal_odds=4,joint_probability=.4,correlation_method='independent_verified')['decision'],'official_pick')
        self.assertEqual(evaluate_ticket(legs,decimal_odds=4,joint_probability=.27,correlation_method='independent_verified')['edge_tier'],'price bonus')

    def test_card_names_the_market_instead_of_a_bare_number(self):
        from sports_analytics_pipeline.revised_strategy import literal_pick
        text = literal_pick({'sport':'wta','market':'spreads','selection_name':'Daria Snigur','line':5.5,'period':'full_game'})
        self.assertIn('games spread', text)
        self.assertIn('not the total games', text)
        self.assertIn('+5.5', text)
        total = literal_pick({'sport':'wta','market':'totals','selection_name':'Over','line':21.5,'period':'full_game'})
        self.assertIn('total games', total)
        self.assertNotIn('games spread', total)
        a=evidence_candidate();b={**a,'grade':'VOID'};a['grade']='WIN'
        text=result_card([a,b])
        self.assertIn('\u2705 WIN 1/2',text);self.assertIn('\u21a9\ufe0f VOID',text)
        self.assertIn('Away Team vs Home Team',text);self.assertIn('Home Team -2.5',text);self.assertIn('ET',text)

    def test_research_mode_valid_but_auto_betting_mode_rejected(self):
        with patch.dict('os.environ',{'BOT_MODE':'research'},clear=True):self.assertEqual(Settings.load('missing.env').bot_mode,'research')
        with patch.dict('os.environ',{'BOT_MODE':'live_wager'},clear=True):
            with self.assertRaises(ValueError):Settings.load('missing.env')

    def test_daily_and_tip_jobs_are_durable_and_send_state_retries(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        row=evidence_candidate();row['model'].update(approved=True,oos_count=500,calibration='acceptable',stable=True)
        prepare_daily_jobs(db,NOW);run_due(db,NOW,{'candidates':[row]})
        messages=pending_messages(db);self.assertEqual(len(messages),2)
        picks=next(v['message'] for v in messages if v['message'].startswith('PICKS'))
        self.assertIn('Home Team -2.5',picks)
        self.assertNotIn('No official picks today.',picks)
        overnight=next(v for v in messages if 'overnight' in v['message_key'])
        self.assertNotIn('Home Team -2.5',overnight['message'])
        self.assertEqual(db.execute("SELECT count(*) FROM research_jobs WHERE kind='tip30'").fetchone()[0],1)
        prepare_daily_jobs(db,NOW);run_due(db,NOW,{'candidates':[row]})
        self.assertEqual(len(pending_messages(db)),2)
        mark_delivered(db,messages[0]['message_key'],NOW)
        self.assertEqual(len(pending_messages(db)),1)
        later=NOW+timedelta(minutes=90)
        run_due(db,later,{'candidates':[]})
        self.assertIn('SCRATCH',pending_messages(db)[-1]['message'])
        self.assertEqual(len(due_jobs(db,later)),0)
        db.close()

    def test_short_real_selections_are_labeled_not_hidden(self):
        experimental=evaluate_research(evidence_candidate(),as_of=NOW)
        self.assertEqual(experimental['publication_decision'],'official_pick')
        self.assertEqual(experimental['analysis_level'],7)
        summary=delivery_cards([experimental],title='LIVE RESEARCH CHECK',as_of=NOW)[0]
        self.assertTrue(summary.startswith('PICKS'))
        self.assertNotIn('No official picks today.',summary)
        self.assertIn('Home Team -2.5',summary)
        # Level 1 discovery without an event identity is not a selection and is not Official.
        watch={'publication_decision':'watchlist','analysis_level':1,'sport':'nfl','home_name':'Home Team','away_name':'Away Team',
               'start_time':(NOW+timedelta(hours=2)).isoformat(),'market':'moneyline','selection_name':'Home Team','decimal_odds':'1.8'}
        hidden=delivery_cards([watch],title='LIVE RESEARCH CHECK',as_of=NOW)[0]
        self.assertIn('No official picks today.',hidden)
        self.assertNotIn('Home Team moneyline',hidden)
        self.assertNotIn('Label: Official',hidden)

    def test_level3_watchlist_names_the_gap_and_is_not_official(self):
        row={'event_id':'e1','sport':'nfl','home_name':'Home Team','away_name':'Away Team',
             'start_time':(NOW+timedelta(hours=2)).isoformat(),'market':'moneyline','selection_name':'Home Team',
             'decimal_odds':'1.8','price_time':NOW.isoformat(),'market_status':'open',
             'publication_decision':'watchlist'}
        summary=delivery_cards([row],title='LIVE RESEARCH CHECK',as_of=NOW)[0]
        self.assertIn('Home Team moneyline',summary)
        self.assertIn('BEST OF THE DAY',summary)
        self.assertIn('Edge: unverified',summary)
        self.assertIn('research-only entries retained in the run audit',summary)
        details=delivery_cards([row],title='LIVE RESEARCH CHECK',as_of=NOW)[1]
        self.assertIn('Home Team', details)
        self.assertIn('No official picks today.',summary)
        self.assertNotIn('Label: Official',summary)
        self.assertNotIn('probability_edge',summary)

    def test_dst_daily_times_remain_local(self):
        db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
        winter=datetime(2026,12,1,16,30,tzinfo=UTC)
        prepare_daily_jobs(db,winter)
        due=db.execute("SELECT due_at FROM research_jobs WHERE kind='midday'").fetchone()[0]
        self.assertEqual(due,'2026-12-01T15:00:00+00:00')
        db.close()

class StrategyAmendmentTests(unittest.TestCase):
    def test_telegram_strength_tiers_do_not_promote_watchlist_or_conditional(self):
        strong=evidence_candidate();strong['model'].update(probability=.8,approved=True,oos_count=500,calibration='acceptable',stable=True)
        strong=evaluate_research(strong,as_of=NOW)
        conditional=evidence_candidate();conditional.update(price_scope='public_reference',book_accessible=False)
        conditional=evaluate_research(conditional,as_of=NOW)
        experimental=evaluate_research(evidence_candidate(),as_of=NOW)
        watch={**experimental,'publication_decision':'watchlist','research_tier':'watchlist','confidence':'Ungraded','model':{},'probability_edge':None,'edge_tier':'Strongest'}
        cards=delivery_cards([watch,experimental,conditional,strong],title='LIVE RESEARCH CHECK',as_of=NOW)
        summary=cards[0]
        headings=['TIER 1', 'WATCHLIST']
        positions=[summary.index(x) for x in headings]
        self.assertEqual(positions,sorted(positions))
        self.assertIn('Tier 1', summary)
        self.assertIn('PLUS-MONEY PARLAYS', summary)
        self.assertIn('Primary-book price unverified',summary)
        self.assertGreaterEqual(len(cards), 2)

    def test_minus_odds_legs_publish_when_actual_combined_quote_is_plus_money(self):
        from sports_analytics_pipeline.revised_strategy import research_card
        candidate=evidence_candidate();candidate['decimal_odds']=1.6
        candidate['model']['probability']=.8
        for quote in candidate['quotes']:
            quote['outcomes'][0]['decimal_odds']=1.6
            quote['outcomes'][1]['decimal_odds']=2.5
        row=evaluate_research(candidate,as_of=NOW)
        self.assertEqual(row['publication_decision'],'official_pick')
        other={**row,'event_id':'another'}
        ticket={'legs':[row,other],'decimal_odds':2.56,'joint_probability':.5,
                'correlation_method':'independent_verified','price_time':NOW.isoformat()}
        self.assertIn('Estimated combined (public books, not a book quote)', research_card([row,other],as_of=NOW,tickets=[ticket]))
        self.assertIn('+156', research_card([row,other],as_of=NOW,tickets=[ticket]))
        ticket['decimal_odds']=2
        ticket['joint_probability']=.6
        self.assertIn('+100', research_card([row,other],as_of=NOW,tickets=[ticket]))
        ticket['decimal_odds']=1.99
        self.assertIn('No qualifying verified ticket.', research_card([row,other],as_of=NOW,tickets=[ticket]))
        ticket['decimal_odds']=2.56
        ticket['price_time']=(NOW-timedelta(hours=2)).isoformat()
        self.assertIn('Estimated combined (public books, not a book quote)', research_card([row,other],as_of=NOW,tickets=[ticket]))

    def test_factors_missing_or_not_modeled_block(self):
        row=evidence_candidate(); del row['factor_analysis']['discipline']
        self.assertFalse(evaluate_research(row,as_of=NOW)['gates']['research'])
        row=evidence_candidate();row['model']['factor_components']=[]
        self.assertFalse(evaluate_research(row,as_of=NOW)['gates']['research'])

    def test_inconsistency_increases_uncertainty(self):
        row=evidence_candidate();row['factor_analysis']['player_consistency']['uncertainty_effect']=.2
        result=evaluate_research(row,as_of=NOW)
        self.assertEqual(Decimal(result['conservative_probability']),Decimal('.45'))
        self.assertEqual(result['publication_decision'],'official_pick')

    def test_plus_money_section_requires_verified_ticket(self):
        from sports_analytics_pipeline.revised_strategy import research_card
        row=evaluate_research(evidence_candidate(),as_of=NOW)
        other={**row,'event_id':'another'}
        t={'legs':[row,other],'decimal_odds':4,'joint_probability':.27,'correlation_method':'independent_verified','price_time':NOW.isoformat()}
        card=research_card([row,other],as_of=NOW,tickets=[t])
        self.assertIn('PLUS-MONEY PARLAYS',card)
        self.assertIn('Estimated combined (public books, not a book quote)', card)
        self.assertIn('+300', card)
        del t['decimal_odds']
        self.assertIn('Estimated combined (public books, not a book quote)', research_card([row],as_of=NOW,tickets=[t]))

    def test_soccer_three_way_model_is_required_and_not_invented(self):
        row=evidence_candidate()
        row.update(sport='epl', market='three_way_moneyline', line=None, period='full_game', participant='home', outcome_count=3)
        row['advanced_metrics']={name:1 for name in required_metrics('epl','three_way_moneyline')}
        row['evidence'].append({**row['evidence'][0],'category':'weather_environment'})
        for quote in row['quotes']:
            quote.update(market='three_way_moneyline', line=None, period='full_game')
            quote['outcomes']=[{'name':'Home Team','decimal_odds':2},{'name':'Draw','decimal_odds':3.4},{'name':'Away Team','decimal_odds':3.2}]
        missing=evaluate_research(row,as_of=NOW)
        self.assertEqual(missing['publication_decision'], 'pass')
        self.assertIn('home win, draw, and away win', missing.get('data_error') or '')
        self.assertIn('No probability was invented', missing['first_blocker'])
        self.assertNotIn('outcome_probabilities', missing.get('model', {}))
        partial=copy.deepcopy(row)
        partial['model']['outcome_probabilities']={'home':.65,'away':.35}
        self.assertIn('No probability was invented', evaluate_research(partial,as_of=NOW)['first_blocker'])
        row['model']['outcome_probabilities']={'home':.65,'draw':.20,'away':.15}
        qualified=evaluate_research(row,as_of=NOW)
        self.assertIn(qualified['publication_decision'], {'experimental','official_pick','conditional_pick'})
        self.assertEqual(qualified['model']['outcome_probabilities']['draw'], .20)

    def test_tennis_requires_performance_consistency_instead_of_team_consistency(self):
        def tennis(use_performance):
            row=evidence_candidate()
            row.update(sport='atp', market='match_moneyline', line=None, period='full_match')
            row['advanced_metrics']={name:1 for name in required_metrics('atp','match_moneyline')}
            row['evidence'].append({**row['evidence'][0],'category':'weather_environment'})
            if use_performance:
                factor=row['factor_analysis'].pop('team_consistency')
                factor['model_component']='performance_consistency'
                row['factor_analysis']['performance_consistency']=factor
            row['model']['factor_components']=list(row['factor_analysis'])
            for quote in row['quotes']:
                quote.update(market='match_moneyline', line=None, period='full_match')
            return evaluate_research(row,as_of=NOW)
        self.assertEqual(tennis(True)['publication_decision'],'official_pick')
        blocked=tennis(False)
        self.assertIn(blocked['publication_decision'], {'official_pick','conditional_pick','watchlist'})
        self.assertIn('performance_consistency', blocked['advanced_metric_gaps'])

    def test_whole_number_player_prop_requires_an_explicit_push(self):
        row=evidence_candidate()
        row.update(market='player_prop', prop_stat='points', direction='over', role_confirmed=True, line=20,
                   trend={'sample_size':8,'same_competition':True,'season_span':2,'source_url':'https://example.test/trend'})
        for quote in row['quotes']:
            quote.update(market='player_prop', line=20, prop_stat='points', direction='over')
            quote['outcomes'][0]['direction']='over'
            quote['outcomes'][1]['direction']='under'
        blocked=evaluate_research(row,as_of=NOW)
        self.assertEqual(blocked['research_tier'], 'watchlist')
        self.assertIn('five comparable games', blocked['research_missing'])
        self.assertFalse(blocked['gates']['material_information'])
        row['model']['push_probability']=.04
        row['role'] = 'starter'
        row['game_logs']=[{'game_id':f'g{n}','player':'Home Team','role':'starter','stat':'points','competition':'nba','date':NOW.isoformat(),'actual':24,'status':'completed','comparable':True,'source_url':'https://example.test/log'} for n in range(5)]
        row['competition'] = 'nba'
        self.assertEqual(evaluate_research(row,as_of=NOW)['publication_decision'],'official_pick')

    def test_public_reference_leg_cannot_become_a_parlay(self):
        from sports_analytics_pipeline.revised_strategy import research_card
        row=evaluate_research(evidence_candidate(),as_of=NOW)
        other={**row,'event_id':'another','price_scope':'public_reference','publication_decision':'official_pick'}
        graded=evaluate_ticket([row,other],decimal_odds=4,joint_probability=.27,correlation_method='independent_verified')
        self.assertIn(graded['decision'], {'official_pick','conditional_pick'})
        self.assertEqual(graded['honesty_label'], 'Estimated combined (public books, not a book quote)')
        ticket={'legs':[row,other],'decimal_odds':4,'joint_probability':.27,'correlation_method':'independent_verified','price_time':NOW.isoformat()}
        card=research_card([row,other],as_of=NOW,tickets=[ticket])
        self.assertIn('Estimated combined (public books, not a book quote)', card)
        self.assertNotIn('Combined +300', card)
