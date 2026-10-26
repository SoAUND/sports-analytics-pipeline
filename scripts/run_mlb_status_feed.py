#!/usr/bin/env python3
"""Render structured GitHub issue comments for the MLB paper status feed."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.mlb_status_feed import (
    build_gate_payload,
    build_result_payload,
    render_status_comment,
)


def _write(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate = subparsers.add_parser("gate")
    gate.add_argument("--workflow-run-id", required=True)
    gate.add_argument("--workflow-run-attempt", required=True)
    gate.add_argument("--operation-date", required=True)
    gate.add_argument("--automated", required=True)
    gate.add_argument("--run-mode", required=True)
    gate.add_argument("--checked-at", required=True)
    gate.add_argument("--output", required=True)

    result = subparsers.add_parser("result")
    result.add_argument("--database", required=True)
    result.add_argument("--workflow-run-id", required=True)
    result.add_argument("--workflow-run-attempt", required=True)
    result.add_argument("--operation-date", required=True)
    result.add_argument("--automated", required=True)
    result.add_argument("--checked-at", required=True)
    result.add_argument("--job-status", required=True)
    result.add_argument("--canonical-outcome", required=True)
    result.add_argument("--output", required=True)

    args = parser.parse_args()
    if args.command == "gate":
        payload = build_gate_payload(
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            operation_date=args.operation_date,
            automated=args.automated,
            run_mode=args.run_mode,
            checked_at=args.checked_at,
        )
    else:
        payload = build_result_payload(
            database_path=args.database,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            operation_date=args.operation_date,
            automated=args.automated,
            checked_at=args.checked_at,
            job_status=args.job_status,
            canonical_outcome=args.canonical_outcome,
        )
    text = render_status_comment(payload)
    _write(args.output, text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
