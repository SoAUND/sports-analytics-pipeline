#!/usr/bin/env python3
"""Verify or manifest the persistent MLB paper SQLite database."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.mlb_persistence import (
    validate_mlb_paper_database,
    verify_database_manifest,
    write_database_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--database", required=True)
    verify.add_argument("--manifest")
    verify.add_argument("--allow-legacy-without-manifest", action="store_true")
    verify.add_argument("--require-model", action="store_true")

    manifest = sub.add_parser("manifest")
    manifest.add_argument("--database", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument("--run-id")

    args = parser.parse_args()
    if args.command == "verify":
        if args.manifest and Path(args.manifest).is_file():
            output = verify_database_manifest(
                args.database,
                args.manifest,
                require_model_artifact=args.require_model,
            )
        elif args.manifest and not args.allow_legacy_without_manifest:
            raise SystemExit("Database manifest is required but missing")
        else:
            output = {
                "status": "ok",
                "legacy_without_manifest": bool(args.manifest),
                "health": validate_mlb_paper_database(
                    args.database,
                    require_model_artifact=args.require_model,
                ),
            }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0

    output = write_database_manifest(
        args.database,
        args.output,
        source_run_id=args.run_id,
    )
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
