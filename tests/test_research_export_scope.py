import unittest
from sports_analytics_pipeline.research_export import execution_price_scope


class ExecutionPriceScopeTests(unittest.TestCase):
    def test_exact_florida_profile_is_execution_not_public_reference(self):
        self.assertEqual(execution_price_scope('hardrockbet_fl',('hardrockbet_fl',)), 'execution')
        self.assertEqual(execution_price_scope('hardrockbet',('hardrockbet',)), 'execution')

    def test_wrong_jurisdiction_and_public_book_remain_conditional(self):
        self.assertEqual(execution_price_scope('hardrockbet_az',('hardrockbet_fl',)), 'public_reference')
        self.assertEqual(execution_price_scope('draftkings',('draftkings',)), 'public_reference')
