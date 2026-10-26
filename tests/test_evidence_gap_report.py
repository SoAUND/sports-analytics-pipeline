import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sports_analytics_pipeline.evidence_gap_report import evidence_gaps, gap_lines


class EvidenceGapReportTests(unittest.TestCase):
    def test_names_exact_team_scopes_and_sport_inputs(self):
        row={'sport':'nfl','home_name':'Cleveland Browns','away_name':'Pittsburgh Steelers'}
        report=evidence_gaps(row)
        injuries=[g for g in report if g['category']=='injury_availability']
        self.assertEqual({g['scope'] for g in injuries},{'Cleveland Browns','Pittsburgh Steelers'})
        self.assertIn('starting quarterback',' '.join(gap_lines(row)))
        self.assertIn('independent win-probability estimate',' '.join(gap_lines(row)))

    def test_retrieved_page_is_not_reported_as_verified_injury_data(self):
        row={'sport':'nfl','public_source_attempts':[{'source_key':'espn-injuries:nfl','status':'http_202','checked_at':'2026-10-01T23:00:00Z'},
             {'source_key':'nfl-official-injuries','category':'injury_availability','status':'retrieved','checked_at':'2026-10-01T23:01:00Z'}]}
        text='\n'.join(gap_lines(row))
        self.assertIn('http_202',text)
        self.assertIn('page retrieved; required game-specific fields remain unverified',text)
        self.assertIn('2026-10-01T23:01:00Z',text)

    def test_nhl_requires_goalie_and_mma_requires_weights(self):
        self.assertIn('confirmed starting goalie',' '.join(gap_lines({'sport':'nhl'})))
        self.assertIn('official weigh-in weights for both fighters',' '.join(gap_lines({'sport':'mma'})))

    def test_telegram_details_are_sent_in_full_without_truncation(self):
        path=Path(__file__).resolve().parents[1]/'scripts/run_revised_research.py'
        spec=importlib.util.spec_from_file_location('detailed_research_sender',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        text=''.join(f'Exact missing field {n}\n' for n in range(400))
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'card.txt';file.write_text(text)
            with patch.object(module.subprocess,'run') as send:
                module.send_card(file)
            parts=[Path(call.args[0][call.args[0].index('--file')+1]).read_text() for call in send.call_args_list]
            self.assertGreater(len(parts),1)
            self.assertEqual(''.join(parts),text)
            self.assertTrue(all(len(p)<=3200 for p in parts))

    def test_each_detail_part_repeats_matchup_header(self):
        path=Path(__file__).resolve().parents[1]/'scripts/run_revised_research.py'
        spec=importlib.util.spec_from_file_location('identified_sender',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        header='RESEARCH DETAILS | WTA | Player A vs Player B | Selection: Player A'
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'card.txt';file.write_text(header+'\n'+('Missing exact input\n'*500))
            with patch.object(module.subprocess,'run') as send:module.send_card(file)
            parts=[Path(call.args[0][call.args[0].index('--file')+1]).read_text() for call in send.call_args_list]
            self.assertGreater(len(parts),1)
            self.assertTrue(all(p.startswith(header) and len(p)<=3200 for p in parts))


if __name__=='__main__':unittest.main()
