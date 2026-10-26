#!/usr/bin/env python3
"""Build the separate test-only Win-Rate-First Telegram report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sports_analytics_pipeline.win_rate_reporting import build_win_rate_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--out-file", required=True)
    args = parser.parse_args()

    status_path = Path(args.status_file)
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Multi-sport status must be a JSON object")
    output = Path(args.out_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_win_rate_report(payload) + "\n", encoding="utf-8")
    print(json.dumps({"status": "written", "path": str(output)}, sort_keys=True))


if __name__ == "__main__":
    main()
