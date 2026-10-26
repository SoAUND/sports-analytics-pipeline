#!/usr/bin/env python3
"""Create our Phase 1 SQLite database and controlled mappings."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC))

from sports_analytics_pipeline.data.db import connect, initialize_schema, required_table_names
from sports_analytics_pipeline.data.seed import seed_reference_data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="var/sports_analytics.sqlite3")
    args = parser.parse_args()

    database = Path(args.database)
    connection = connect(database)
    initialize_schema(connection)
    seed_reference_data(connection)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = required_table_names() - tables
    if missing:
        raise RuntimeError(f"Missing required tables: {sorted(missing)}")
    team_count = connection.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    stadium_count = connection.execute("SELECT COUNT(*) FROM stadiums").fetchone()[0]
    print(f"Created {database} with {team_count} teams and {stadium_count} stadiums.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
