"""Match provider final scores to stored events and persist named-source results."""

from __future__ import annotations

import sqlite3

from .collectors.multisport_scores import ScoreBatch
from .multisport_results import record_result


def persist_score_batch(connection: sqlite3.Connection, batch: ScoreBatch) -> dict[str, object]:
    stored = 0
    skipped = 0
    errors: list[str] = []
    for score in batch.scores:
        row = connection.execute(
            """
            SELECT event_id FROM multisport_events
            WHERE sport=? AND provider_sport_key=? AND source_event_id=?
            """,
            (score.sport, score.provider_sport_key, score.source_event_id),
        ).fetchone()
        if row is None:
            skipped += 1
            errors.append(f"Unmatched source event: {score.source_event_id}")
            continue
        try:
            record_result(
                connection,
                event_id=str(row["event_id"]),
                result_status="final",
                source_key="the_odds_api",
                source_updated_at=score.source_updated_at,
                collected_at=score.collected_at,
                home_score=score.home_score,
                away_score=score.away_score,
                winner_role=score.winner_role,
                raw_result=score.raw_result,
            )
            stored += 1
        except Exception as exc:
            errors.append(f"{score.source_event_id}: {exc}")
    return {"stored": stored, "skipped": skipped, "errors": tuple(errors)}
