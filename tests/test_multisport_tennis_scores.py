import unittest
from datetime import datetime, timezone

from sports_analytics_pipeline.collectors.multisport_scores import parse_scores


UTC = timezone.utc
NOW = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)


def completed_score(*, sport_key, event_id, home, away, home_score, away_score):
    return {
        "id": event_id,
        "sport_key": sport_key,
        "commence_time": "2026-09-01T20:15:00Z",
        "completed": True,
        "home_team": home,
        "away_team": away,
        "scores": [
            {"name": home, "score": str(home_score)},
            {"name": away, "score": str(away_score)},
        ],
        "last_update": "2026-09-02T01:00:00Z",
    }


class TennisScoreResultTests(unittest.TestCase):
    def test_atp_us_open_regulation_final_is_accepted(self):
        key = "tennis_atp_us_open"
        batch = parse_scores(
            [
                completed_score(
                    sport_key=key,
                    event_id="atp-1",
                    home="Jan Choinski",
                    away="Botic van de Zandschulp",
                    home_score=1,
                    away_score=3,
                )
            ],
            sport="atp",
            provider_sport_key=key,
            collected_at=NOW,
        )
        self.assertEqual(len(batch.scores), 1)
        score = batch.scores[0]
        self.assertEqual(score.source_event_id, "atp-1")
        self.assertEqual(score.winner_role, "away")
        self.assertTrue(score.raw_result["first_set_completed"])
        self.assertTrue(score.raw_result["at_least_one_point_completed"])

    def test_wta_regulation_final_is_accepted(self):
        key = "tennis_wta_us_open"
        batch = parse_scores(
            [
                completed_score(
                    sport_key=key,
                    event_id="wta-1",
                    home="Magda Linette",
                    away="Francesca Jones",
                    home_score=2,
                    away_score=0,
                )
            ],
            sport="wta",
            provider_sport_key=key,
            collected_at=NOW,
        )
        self.assertEqual(len(batch.scores), 1)
        self.assertEqual(batch.scores[0].winner_role, "home")

    def test_ambiguous_tennis_completion_fails_closed(self):
        key = "tennis_atp_us_open"
        batch = parse_scores(
            [
                completed_score(
                    sport_key=key,
                    event_id="atp-retirement",
                    home="Player One",
                    away="Player Two",
                    home_score=2,
                    away_score=0,
                )
            ],
            sport="atp",
            provider_sport_key=key,
            collected_at=NOW,
        )
        self.assertEqual(batch.scores, ())
        self.assertIn("retirement/walkover", batch.warnings[0])


if __name__ == "__main__":
    unittest.main()
