#!/usr/bin/env python3
"""Promote approved pregame pass records into non-countable test observations."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_operations import bootstrap


UTC = timezone.utc


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Capture times must include a time zone")
    return parsed.astimezone(UTC)


def capture_observation(connection, observation: dict[str, object]) -> str:
    event_id = str(observation["event_id"])
    prediction_id = str(observation["prediction_id"])
    capture_time = _time(str(observation["capture_time"]))
    odds_snapshot_id = int(observation["odds_snapshot_id"])
    outcome_role = str(observation["outcome_role"])
    source_run_url = str(observation["source_run_url"])

    event = connection.execute(
        "SELECT * FROM multisport_events WHERE event_id=?", (event_id,)
    ).fetchone()
    prediction = connection.execute(
        "SELECT * FROM multisport_predictions WHERE prediction_id=? AND event_id=?",
        (prediction_id, event_id),
    ).fetchone()
    odds = connection.execute(
        """
        SELECT * FROM multisport_odds_snapshots
        WHERE odds_snapshot_id=? AND event_id=? AND outcome_role=?
        """,
        (odds_snapshot_id, event_id, outcome_role),
    ).fetchone()
    decision = connection.execute(
        """
        SELECT * FROM multisport_paper_decisions
        WHERE event_id=? AND prediction_id=?
        ORDER BY check_time
        LIMIT 1
        """,
        (event_id, prediction_id),
    ).fetchone()
    if event is None or prediction is None or odds is None or decision is None:
        raise ValueError(f"Required pregame evidence is missing for {event_id}")
    if str(decision["confidence_label"] or "") == "Unqualified Test Observation":
        return "already_captured"
    if str(decision["decision_type"]) != "pass":
        raise ValueError(f"Existing decision is not a pass for {event_id}")
    if capture_time >= _time(str(event["commence_time"])):
        raise ValueError(f"Capture time is not pregame for {event_id}")
    if _time(str(prediction["prediction_time"])) > capture_time:
        raise ValueError(f"Prediction was created after capture time for {event_id}")
    if _time(str(odds["collected_at"])) > capture_time:
        raise ValueError(f"Odds were collected after capture time for {event_id}")

    probabilities = json.loads(str(prediction["probabilities_json"]))
    model_probability = float(probabilities[outcome_role])
    uncertainty = float(prediction["uncertainty_margin"])
    conservative_probability = max(0.0, model_probability - uncertainty)
    decimal_odds = float(odds["decimal_odds"])
    break_even_probability = 1.0 / decimal_odds
    probability_edge = conservative_probability - break_even_probability
    ev_per_dollar = conservative_probability * decimal_odds - 1.0
    original_pass_reason = str(decision["pass_reason"] or "Qualification gates failed.")
    reasons = [
        "Captured from a locked pregame prediction and odds snapshot.",
        "Owner-directed test observation: preserve the opportunity even when it does not qualify.",
        "This record does not increase training examples or OOS validation.",
        f"Source run: {source_run_url}",
        f"Original gate result: {original_pass_reason}",
    ]
    loss_reasons = [
        "The engineering market baseline may be wrong.",
        "Missing research is why this record did not qualify.",
    ]
    connection.execute(
        """
        UPDATE multisport_paper_decisions
        SET outcome_role=?,
            outcome_participant_id=?,
            sportsbook_key=?,
            odds_snapshot_id=?,
            points=?,
            decimal_odds=?,
            check_time=?,
            break_even_probability=?,
            market_consensus_probability=NULL,
            model_probability=?,
            uncertainty_margin=?,
            conservative_probability=?,
            probability_edge=?,
            ev_per_dollar=?,
            minimum_acceptable_decimal_odds=NULL,
            pick_strength_score=?,
            confidence_label='Unqualified Test Observation',
            decision_type='test_only_selection',
            reasons_json=?,
            loss_reasons_json=?,
            pass_reason=?,
            manual_approval_status='pending',
            countable_oos=0,
            rule_version='multisport-eval-1.2-owner-shadow-test'
        WHERE paper_decision_id=?
        """,
        (
            outcome_role,
            odds["outcome_participant_id"],
            odds["sportsbook_key"],
            odds_snapshot_id,
            odds["points"],
            decimal_odds,
            capture_time.isoformat(),
            break_even_probability,
            model_probability,
            uncertainty,
            conservative_probability,
            probability_edge,
            ev_per_dollar,
            int(observation.get("diagnostic_score", 0)),
            json.dumps(reasons),
            json.dumps(loss_reasons),
            "Unqualified test observation — original qualification gates failed; captured for testing only.",
            decision["paper_decision_id"],
        ),
    )
    return "captured"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()

    payload = json.loads(Path(args.ledger).read_text(encoding="utf-8"))
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise SystemExit("Observation ledger must contain a non-empty observations list")

    connection = connect(args.database)
    try:
        initialize_schema(connection)
        bootstrap(connection, as_of=max(_time(str(row["capture_time"])) for row in observations))
        results = {
            str(row["event_id"]): capture_observation(connection, row)
            for row in observations
        }
        connection.commit()
    finally:
        connection.close()
    print(json.dumps({"ledger": args.ledger, "results": results}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
