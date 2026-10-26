"""MLB Phase 7 operations orchestration for repeatable paper-testing runs.

The coordinator is deliberately fail-closed and test-only. It sequences the
existing collectors, model/evaluation, paper tracker, settlement, reporting,
and validation review without creating any wagering capability.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Callable, Iterable, Sequence


@dataclass(frozen=True)
class OperationStep:
    name: str
    command: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class StepResult:
    name: str
    status: str
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), cwd=cwd, capture_output=True, text=True, check=False
    )


def build_daily_mlb_plan(
    *,
    date: str,
    env_path: str = ".env",
    hours: int = 24,
    skip_collection: bool = False,
    skip_settlement: bool = False,
) -> tuple[OperationStep, ...]:
    if len(date) != 10:
        raise ValueError("date must use YYYY-MM-DD")
    if hours < 1 or hours > 168:
        raise ValueError("hours must be between 1 and 168")

    py = sys.executable
    steps: list[OperationStep] = []
    if not skip_collection:
        steps.append(OperationStep("collect", (py, "scripts/run_mlb_collectors.py", date, "--env", env_path)))
    steps.extend(
        [
            OperationStep(
                "model",
                (
                    py, "scripts/run_mlb_model.py", "--env", env_path, "run",
                    "--hours", str(hours), "--require-readiness",
                    "--operation-date", date,
                ),
            ),
            OperationStep(
                "evaluate_track",
                (
                    py, "scripts/run_mlb_tracker.py", "--skip-pull",
                    "--hours", str(hours), "--env", env_path,
                    "--require-readiness", "--operation-date", date,
                    "--model-runs-already-created",
                ),
            ),
        ]
    )
    if not skip_settlement:
        steps.append(OperationStep("settle", (py, "scripts/run_mlb_settle.py", date, "--env", env_path)))
    steps.extend(
        [
            OperationStep("report", (py, "scripts/run_mlb_report.py", "--env", env_path)),
            OperationStep("review", (py, "scripts/run_mlb_review.py", "--env", env_path)),
        ]
    )
    return tuple(steps)


def _truncate(value: str, limit: int = 12000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _insert_run(db: sqlite3.Connection, *, run_id: str, operation_date: str, started_at: datetime) -> None:
    db.execute(
        """
        INSERT INTO mlb_operations_runs(
            run_id, operation_date, mode, status, started_at, completed_at,
            failed_step, summary_json
        ) VALUES (?, ?, 'test_only', 'running', ?, NULL, NULL, '{}')
        """,
        (run_id, operation_date, started_at.astimezone(timezone.utc).isoformat()),
    )
    db.commit()


def _finish_run(
    db: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    completed_at: datetime,
    failed_step: str | None,
    summary: dict,
) -> None:
    db.execute(
        """
        UPDATE mlb_operations_runs
        SET status=?, completed_at=?, failed_step=?, summary_json=?
        WHERE run_id=?
        """,
        (
            status,
            completed_at.astimezone(timezone.utc).isoformat(),
            failed_step,
            json.dumps(summary, sort_keys=True),
            run_id,
        ),
    )
    db.commit()


def execute_daily_mlb_operations(
    db: sqlite3.Connection,
    *,
    project_root: Path,
    operation_date: str,
    bot_mode: str,
    steps: Iterable[OperationStep],
    runner: Runner = default_runner,
    now: datetime | None = None,
) -> dict:
    """Execute a prebuilt operations plan sequentially and fail closed.

    Any required step failure stops all later steps. The database receives one
    immutable run summary suitable for diagnosing missed or partial daily runs.
    """
    if bot_mode not in {"research", "test_only"}:
        raise ValueError("MLB Phase 7 requires a supported research mode")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a time zone")

    run_id = str(uuid.uuid4())
    _insert_run(db, run_id=run_id, operation_date=operation_date, started_at=now)
    results: list[dict] = []
    failed_step: str | None = None

    for step in tuple(steps):
        completed = runner(step.command, project_root)
        result = StepResult(
            name=step.name,
            status="succeeded" if completed.returncode == 0 else "failed",
            returncode=int(completed.returncode),
            stdout=_truncate(completed.stdout or ""),
            stderr=_truncate(completed.stderr or ""),
        )
        results.append(result.__dict__)
        if result.status == "failed" and step.required:
            failed_step = step.name
            break

    status = "failed" if failed_step else "succeeded"
    summary = {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 7,
        "operation_date": operation_date,
        "status": status,
        "failed_step": failed_step,
        "steps": results,
        "automatic_wagering": False,
        "automatic_model_approval": False,
        "notice": (
            "Phase 7 automates the paper-testing workflow only. A failed required step stops later work; "
            "no real-money action, stake, or automatic model approval is ever created."
        ),
    }
    _finish_run(
        db,
        run_id=run_id,
        status=status,
        completed_at=datetime.now(timezone.utc),
        failed_step=failed_step,
        summary=summary,
    )
    summary["run_id"] = run_id
    return summary


def latest_mlb_operations_summary(db: sqlite3.Connection) -> dict | None:
    row = db.execute(
        """
        SELECT run_id, operation_date, mode, status, started_at, completed_at,
               failed_step, summary_json
        FROM mlb_operations_runs
        ORDER BY started_at DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["summary_json"])
    payload.update(
        {
            "run_id": row["run_id"],
            "operation_date": row["operation_date"],
            "mode": row["mode"],
            "status": row["status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "failed_step": row["failed_step"],
        }
    )
    return payload

