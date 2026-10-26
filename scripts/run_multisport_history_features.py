#!/usr/bin/env python3
"""Run leakage-safe historical feature import/build for non-EPL multi-sport models."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sports_analytics_pipeline.config import Settings
from sports_analytics_pipeline.data.db import connect, initialize_schema
from sports_analytics_pipeline.multisport_history_features import (
    SUPPORTED_SPORTS,
    build_history_examples,
    import_feature_evidence,
    load_feature_evidence,
)

UTC = timezone.utc


def _sports(value: str) -> tuple[str, ...]:
    rows = tuple(part.casefold().strip() for part in value.split(",") if part.strip())
    if not rows or len(rows) != len(set(rows)) or any(row not in SUPPORTED_SPORTS for row in rows):
        raise argparse.ArgumentTypeError(
            "sports must be a unique comma-separated subset of nba,wnba,mma,atp,wta"
        )
    return rows


def _db(settings: Settings):
    db = connect(settings.database_path)
    initialize_schema(db)
    return db


def command_import(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("Unsupported research mode")
    db = _db(settings)
    try:
        result = import_feature_evidence(
            db,
            load_feature_evidence(args.path),
            collected_at=datetime.now(UTC),
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 1 if result["rejected"] else 0
    finally:
        db.close()


def command_build(args: argparse.Namespace) -> int:
    settings = Settings.load(args.env)
    if settings.bot_mode not in {"research", "test_only"}:
        raise SystemExit("Unsupported research mode")
    db = _db(settings)
    try:
        result = build_history_examples(
            db,
            sports=args.sports,
            built_at=datetime.now(UTC),
            minimum_examples=args.min_examples,
        )
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        db.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--env", default=".env")
    sub = root.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import-evidence")
    importer.add_argument("path")
    importer.set_defaults(func=command_import)
    builder = sub.add_parser("build-examples")
    builder.add_argument("--sports", type=_sports, default=SUPPORTED_SPORTS)
    builder.add_argument("--min-examples", type=int, default=50)
    builder.set_defaults(func=command_build)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

