"""Persistence and recovery checks for our MLB paper-testing database.

This module treats the SQLite database as evidence.  It validates the file
before a live paper run, creates a checksum manifest for successful backups,
and verifies that a restored backup is the same file that was saved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from .data.db import required_table_names
from .mlb_modeling import MODEL_VERSION

MLB_OPERATIONAL_TABLES = {
    "historical_mlb_games",
    "mlb_model_artifacts",
    "mlb_validation_reviews",
    "mlb_operations_runs",
    "mlb_live_paper_runs",
    "mlb_reliability_runs",
    "mlb_final_reviews",
    "mlb_manual_dispositions",
}


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a time zone")
    return value.astimezone(timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
    target = Path(path)
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_mlb_paper_database(
    path: str | Path,
    *,
    require_model_artifact: bool = False,
) -> dict[str, Any]:
    """Validate the persistent SQLite file without mutating it."""
    target = Path(path)
    if not target.is_file():
        raise ValueError("MLB paper database file is missing")
    if target.stat().st_size <= 0:
        raise ValueError("MLB paper database file is empty")

    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise ValueError(f"SQLite integrity check failed: {integrity}")

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected = required_table_names() | MLB_OPERATIONAL_TABLES
        missing = sorted(expected - tables)
        if missing:
            raise ValueError(f"MLB paper database is missing tables: {missing}")

        nfl_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM teams WHERE sport='nfl'"
            ).fetchone()[0]
        )
        mlb_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM teams WHERE sport='mlb'"
            ).fetchone()[0]
        )
        if nfl_count != 32:
            raise ValueError(f"Expected 32 NFL teams, found {nfl_count}")
        if mlb_count != 30:
            raise ValueError(f"Expected 30 MLB teams, found {mlb_count}")

        artifact = connection.execute(
            """
            SELECT model_version, calibration_status, market_audit_status, status
            FROM mlb_model_artifacts
            WHERE model_version=?
            """,
            (MODEL_VERSION,),
        ).fetchone()
        if require_model_artifact and artifact is None:
            raise ValueError(
                "No stored MLB model artifact exists; bootstrap the locked model before a paper run"
            )

        mlb_decisions = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_decisions p
                JOIN games g ON g.game_id=p.game_id
                WHERE g.sport='mlb'
                """
            ).fetchone()[0]
        )
        return {
            "status": "ok",
            "integrity": integrity,
            "nfl_teams": nfl_count,
            "mlb_teams": mlb_count,
            "mlb_paper_decisions": mlb_decisions,
            "model_artifact": None
            if artifact is None
            else {
                "model_version": str(artifact["model_version"]),
                "calibration_status": str(artifact["calibration_status"]),
                "market_audit_status": str(artifact["market_audit_status"]),
                "status": str(artifact["status"]),
            },
        }
    finally:
        connection.close()


def build_database_manifest(
    path: str | Path,
    *,
    created_at: datetime | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    target = Path(path)
    health = validate_mlb_paper_database(target)
    created_at = created_at or datetime.now(timezone.utc)
    return {
        "format": "our-mlb-paper-db-manifest-v1",
        "created_at": _iso(created_at),
        "source_run_id": source_run_id,
        "filename": target.name,
        "size_bytes": target.stat().st_size,
        "sha256": sha256_file(target),
        "health": health,
    }


def write_database_manifest(
    path: str | Path,
    manifest_path: str | Path,
    *,
    created_at: datetime | None = None,
    source_run_id: str | None = None,
) -> dict[str, Any]:
    manifest = build_database_manifest(
        path,
        created_at=created_at,
        source_run_id=source_run_id,
    )
    Path(manifest_path).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_database_manifest(
    path: str | Path,
    manifest_path: str | Path,
    *,
    require_model_artifact: bool = False,
) -> dict[str, Any]:
    target = Path(path)
    manifest_target = Path(manifest_path)
    if not manifest_target.is_file():
        raise ValueError("MLB paper database manifest is missing")
    payload = json.loads(manifest_target.read_text(encoding="utf-8"))
    if payload.get("format") != "our-mlb-paper-db-manifest-v1":
        raise ValueError("Unsupported MLB paper database manifest format")
    if payload.get("filename") != target.name:
        raise ValueError("Database filename does not match its manifest")
    if int(payload.get("size_bytes", -1)) != target.stat().st_size:
        raise ValueError("Database size does not match its manifest")
    expected_hash = str(payload.get("sha256", ""))
    actual_hash = sha256_file(target)
    if not expected_hash or actual_hash != expected_hash:
        raise ValueError("Database checksum does not match its manifest")
    health = validate_mlb_paper_database(
        target,
        require_model_artifact=require_model_artifact,
    )
    return {
        "status": "ok",
        "sha256": actual_hash,
        "manifest_created_at": payload.get("created_at"),
        "health": health,
    }
