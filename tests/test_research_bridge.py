import copy
from datetime import datetime, timezone
import unittest
from sports_analytics_pipeline.research_bridge import projection_evidence


class ResearchBridgeTests(unittest.TestCase):
    def test_only_exact_model_market_and_cutoff_are_carried(self):
        now=datetime(2026,10,4,19,tzinfo=timezone.utc)
        model={'version':'model-v2','prediction_time':now.isoformat()}
        candidate={'event_id':'event','market':'spreads','period':'full_game','line':-2.5,'participant':'home'}
        bundle={**candidate,'model_version':'model-v2','prediction_time':now.isoformat(),
                'advanced_metrics':{'pace':99},'factor_analysis':{'discipline':{'evidence':'observed fouls'}}}
        check={'status':'verified','source_url':'https://example.test/stats','source_updated_at':now.isoformat(),
               'collected_at':now.isoformat(),'data':{'projection_evidence':[bundle]}}
        result=projection_evidence([check],candidate,model,as_of=now)
        self.assertEqual(result['advanced_metrics'],{'pace':99})
        self.assertNotIn('factor_components',result)
        for field,value in [('line',-3.5),('model_version','old-model'),('participant','away')]:
            altered=copy.deepcopy(check);altered['data']['projection_evidence'][0][field]=value
            self.assertEqual(projection_evidence([altered],candidate,model,as_of=now),{})
        for field,value in [('status','conditional'),('source_updated_at','bad'),('collected_at','2026-10-04T20:00:00+00:00')]:
            altered=copy.deepcopy(check);altered[field]=value
            self.assertEqual(projection_evidence([altered],candidate,model,as_of=now),{})

