"""MLB Phase 9 reliability guard for repeatable paper operations.

The reliability layer is intentionally conservative.  It provides one-date run
locking, bounded retries, settlement recheck visibility, and operational health
reporting.  It never creates a real-money action, stake, or model approval.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import json
from pathlib import Path
import sqlite3
import subprocess
import uuid
from typing import Callable, Sequence

Runner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("operation_date must use YYYY-MM-DD") from exc


def resolve_operation_date(
    value: str | None,
    *,
    now: datetime | None = None,
    timezone_name: str = "America/New_York",
) -> str:
    """Resolve an explicit MLB date or use the current U.S. Eastern calendar date."""
    if value:
        _validate_date(value)
        return value
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a time zone")
    return now.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def default_runner(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), cwd=cwd, capture_output=True, text=True, check=False)


def build_reliable_operations_command(*, operation_date: str, env_path: str = ".env", hours: int = 24) -> tuple[str, ...]:
    """Build the existing Phase 7 operations command without adding live wagering."""
    import sys
    _validate_date(operation_date)
    if hours < 1 or hours > 168:
        raise ValueError("hours must be between 1 and 168")
    return (
        sys.executable,
        "scripts/run_mlb_operations.py",
        operation_date,
        "--env",
        env_path,
        "--hours",
        str(hours),
    )


def _latest_for_date(db: sqlite3.Connection, operation_date: str):
    return db.execute(
        """
        SELECT * FROM mlb_reliability_runs
        WHERE operation_date=?
        ORDER BY started_at DESC, rowid DESC LIMIT 1
        """,
        (operation_date,),
    ).fetchone()


def _start_run(db: sqlite3.Connection, *, operation_date: str, now: datetime, stale_after_minutes: int) -> tuple[str | None, dict | None]:
    if stale_after_minutes <= 0:
        raise ValueError("stale_after_minutes must be positive")
    previous = _latest_for_date(db, operation_date)
    now_iso = _iso(now)
    if previous is not None:
        if previous["status"] == "succeeded":
            return None, {
                "mode": "test_only",
                "sport": "mlb",
                "phase": 9,
                "operation_date": operation_date,
                "status": "skipped_duplicate",
                "prior_run_id": previous["reliability_run_id"],
                "automatic_wagering": False,
                "automatic_model_approval": False,
                "notice": "A successful Phase 9 run already exists for this date, so no duplicate paper-operations run was started.",
            }
        if previous["status"] == "running":
            started = datetime.fromisoformat(previous["started_at"])
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if now.astimezone(timezone.utc) - started.astimezone(timezone.utc) <= timedelta(minutes=stale_after_minutes):
                raise RuntimeError("an MLB reliability run is already active for this date")
            db.execute(
                "UPDATE mlb_reliability_runs SET status='abandoned', completed_at=? WHERE reliability_run_id=?",
                (now_iso, previous["reliability_run_id"]),
            )
            db.commit()

    run_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO mlb_reliability_runs(
            reliability_run_id, operation_date, mode, status, attempts,
            started_at, completed_at, attempts_json, summary_json,
            automatic_wagering, automatic_model_approval
        ) VALUES (?, ?, 'test_only', 'running', 0, ?, NULL, '[]', '{}', 0, 0)
        """,
        (run_id, operation_date, now_iso),
    )
    db.commit()
    return run_id, None


def execute_reliable_mlb_operations(
    db: sqlite3.Connection,
    *,
    project_root: Path,
    operation_date: str,
    bot_mode: str,
    command: Sequence[str],
    runner: Runner = default_runner,
    max_attempts: int = 3,
    stale_after_minutes: int = 120,
    now: datetime | None = None,
) -> dict:
    """Run the paper-operations command with a date lock and bounded retries."""
    if bot_mode not in {"research", "test_only"}:
        raise ValueError("MLB Phase 9 requires a supported research mode")
    _validate_date(operation_date)
    if max_attempts < 1 or max_attempts > 5:
        raise ValueError("max_attempts must be between 1 and 5")
    now = now or datetime.now(timezone.utc)
    _iso(now)

    run_id, duplicate = _start_run(
        db, operation_date=operation_date, now=now, stale_after_minutes=stale_after_minutes
    )
    if duplicate is not None:
        return duplicate
    assert run_id is not None

    attempts: list[dict] = []
    final_status = "failed"
    for number in range(1, max_attempts + 1):
        completed = runner(tuple(command), project_root)
        attempts.append({
            "attempt": number,
            "returncode": int(completed.returncode),
            "stdout": (completed.stdout or "")[-12000:],
            "stderr": (completed.stderr or "")[-12000:],
            "status": "succeeded" if completed.returncode == 0 else "failed",
        })
        if completed.returncode == 0:
            final_status = "succeeded"
            break

    completed_at = datetime.now(timezone.utc)
    summary = {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 9,
        "operation_date": operation_date,
        "status": final_status,
        "attempts": len(attempts),
        "attempt_history": attempts,
        "automatic_wagering": False,
        "automatic_model_approval": False,
        "notice": "Phase 9 retries paper operations only. It cannot take real-money actions, recommend stakes, or approve the model.",
    }
    with db:
        db.execute(
            """
            UPDATE mlb_reliability_runs
            SET status=?, attempts=?, completed_at=?, attempts_json=?, summary_json=?
            WHERE reliability_run_id=?
            """,
            (
                final_status,
                len(attempts),
                _iso(completed_at),
                json.dumps(attempts, sort_keys=True),
                json.dumps(summary, sort_keys=True),
                run_id,
            ),
        )
    summary["reliability_run_id"] = run_id
    return summary


def settlement_recheck_count(db: sqlite3.Connection) -> int:
    """Count final MLB test selections that still have no paper settlement."""
    row = db.execute(
        """
        SELECT COUNT(*) AS n
        FROM paper_decisions p
        JOIN games g ON g.game_id=p.game_id
        LEFT JOIN paper_results r ON r.paper_decision_id=p.paper_decision_id
        WHERE g.sport='mlb' AND g.status='final'
          AND p.decision_type='test_only_selection' AND r.paper_result_id IS NULL
        """
    ).fetchone()
    return int(row["n"])


def build_mlb_reliability_health(db: sqlite3.Connection) -> dict:
    latest_rel = db.execute(
        "SELECT * FROM mlb_reliability_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    latest_ops = db.execute(
        "SELECT * FROM mlb_operations_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    latest_live = db.execute(
        "SELECT * FROM mlb_live_paper_runs ORDER BY recorded_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    unsettled = settlement_recheck_count(db)

    warnings: list[str] = []
    if latest_rel is None:
        warnings.append("no_phase9_reliability_run")
    elif latest_rel["status"] != "succeeded":
        warnings.append("latest_reliability_run_not_successful")
    if latest_ops is not None and latest_ops["status"] == "failed":
        warnings.append("latest_operations_run_failed")
    if latest_live is not None and latest_live["status"] == "blocked":
        warnings.append("latest_live_paper_run_blocked")
    if unsettled:
        warnings.append("final_test_selections_need_settlement_recheck")

    return {
        "mode": "test_only",
        "sport": "mlb",
        "phase": 9,
        "status": "healthy" if not warnings else "attention_required",
        "warnings": warnings,
        "unsettled_final_test_selections": unsettled,
        "latest_reliability_status": None if latest_rel is None else latest_rel["status"],
        "latest_operations_status": None if latest_ops is None else latest_ops["status"],
        "latest_live_paper_status": None if latest_live is None else latest_live["status"],
        "automatic_wagering": False,
        "automatic_model_approval": False,
    }


def latest_mlb_reliability_summary(db: sqlite3.Connection) -> dict | None:
    row = db.execute(
        "SELECT * FROM mlb_reliability_runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["summary_json"] or "{}")
    payload.update({
        "reliability_run_id": row["reliability_run_id"],
        "operation_date": row["operation_date"],
        "status": row["status"],
        "attempts": row["attempts"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    })
    return payload

