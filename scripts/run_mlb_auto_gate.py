#!/usr/bin/env python3
"""Resolve manual MLB workflow inputs or gate a scheduled paper run."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sports_analytics_pipeline.mlb_auto import (
    build_automatic_gate,
    candidate_schedule_dates,
    fetch_official_mlb_schedule,
    find_games_in_pregame_window,
)


def _write_outputs(payload: dict) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        for key in ("should_run", "automated", "run_mode", "operation_date", "hours", "attempts"):
            value = payload[key]
            if isinstance(value, bool):
                value = str(value).lower()
            handle.write(f"{key}={value}\n")


def _github_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("GitHub artifact time must include a time zone")
    return parsed.astimezone(timezone.utc)


def _latest_auto_success_at() -> datetime | None:
    """Return the newest successful automatic-run marker from GitHub Actions.

    This lookup is only a quota/cooldown optimization. If GitHub's artifact API
    is temporarily unavailable, the function returns None so the downstream
    database and research safety gates remain the source of truth.
    """
    token = os.environ.get("GITHUB_API_TOKEN") or ""
    repository = os.environ.get("GITHUB_REPOSITORY") or ""
    if not token or not repository:
        return None

    owner_repo = "/".join(quote(part, safe="") for part in repository.split("/", 1))
    url = (
        f"https://api.github.com/repos/{owner_repo}/actions/artifacts"
        "?name=mlb-auto-success&per_page=100"
    )
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "OurSportsBot-MLB-AutoGate",
        },
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        artifacts = payload.get("artifacts") or []
        times = [
            _github_time(str(item["created_at"]))
            for item in artifacts
            if isinstance(item, dict)
            and not item.get("expired", False)
            and item.get("created_at")
        ]
        return max(times) if times else None
    except Exception as exc:  # pragma: no cover - network-specific fallback
        print(
            f"Automatic-run cooldown lookup unavailable; continuing with safety gates: {exc}",
            file=sys.stderr,
        )
        return None


def main() -> int:
    event_name = os.environ.get("EVENT_NAME", "workflow_dispatch")
    now = datetime.now(timezone.utc)

    if event_name == "schedule":
        checks = []
        selected_date = None
        due_games = []
        for operation_date in candidate_schedule_dates(now=now):
            schedule = fetch_official_mlb_schedule(operation_date)
            due = find_games_in_pregame_window(schedule, now=now)
            checks.append({"operation_date": operation_date, "due_games": list(due)})
            if due and selected_date is None:
                selected_date = operation_date
                due_games = list(due)

        if selected_date is None:
            payload = {
                "should_run": False,
                "automated": True,
                "run_mode": "paper_run",
                "operation_date": candidate_schedule_dates(now=now)[0],
                "hours": 2,
                "attempts": 3,
                "due_games": [],
                "schedule_checks": checks,
                "cooldown_active": False,
                "latest_auto_success_at": None,
                "notice": (
                    "No official regular-season MLB game is inside the automatic "
                    "25-65 minute pregame window, so the full paper workflow is skipped."
                ),
            }
        else:
            latest_auto_success_at = _latest_auto_success_at()
            schedule_payload = fetch_official_mlb_schedule(selected_date)
            payload = build_automatic_gate(
                event_name="schedule",
                now=now,
                requested_date=selected_date,
                schedule_payload=schedule_payload,
                latest_auto_success_at=latest_auto_success_at,
            )
            payload["schedule_checks"] = checks
            payload["due_games"] = due_games
    else:
        payload = build_automatic_gate(
            event_name=event_name,
            now=now,
            requested_date=os.environ.get("REQUESTED_DATE") or None,
            requested_mode=os.environ.get("REQUESTED_MODE") or None,
            requested_hours=os.environ.get("REQUESTED_HOURS") or None,
            requested_attempts=os.environ.get("REQUESTED_ATTEMPTS") or None,
        )

    _write_outputs(payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
