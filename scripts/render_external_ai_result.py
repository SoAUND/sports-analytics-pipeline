#!/usr/bin/env python3
"""Render a settled external-AI screenshot ticket without crediting records."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _odds(value):
    if value is None:
        return "Unavailable"
    number = float(value)
    return f"+{number:g}" if number > 0 else f"{number:g}"


def render(payload: dict) -> str:
    if payload.get("credit_to_our_bot") is not False:
        raise ValueError("External-AI tickets must explicitly set credit_to_our_bot=false")
    origin = str(payload.get("origin") or "").strip().casefold()
    if origin in {"", "owner", "our_bot", "system", "recommended"}:
        raise ValueError("A separate external-AI origin is required")
    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        raise ValueError("A non-empty legs array is required")

    provider = " ".join(word.capitalize() for word in origin.split("_"))
    result = str(payload.get("result") or "unsettled").upper()
    lines = [
        f"🤖 EXTERNAL AI RESULT — {provider}",
        "",
        f"Ticket: {str(payload.get('ticket_type') or 'ticket').upper()} | Result: {'✅ WIN' if result == 'WIN' else result}",
        f"Sportsbook: {str(payload.get('sportsbook_key') or 'unknown').upper()}",
        f"Ticket Odds: {_odds(payload.get('offered_american_odds'))}",
        "Official Record: NOT COUNTED",
        "",
    ]
    wins = losses = 0
    for index, leg in enumerate(legs, start=1):
        status = str(leg.get("result") or "unsettled").casefold()
        if status == "win":
            marker = "✅ WIN"
            wins += 1
        elif status == "loss":
            marker = "❌ LOSS"
            losses += 1
        else:
            marker = status.upper()
        lines.extend(
            [
                f"{index}. {leg.get('selection', 'Unknown selection')} ({_odds(leg.get('entry_american_odds'))})",
                f"Event: {leg.get('event_label', 'Unknown event')}",
                f"Result: {marker}",
                f"Final: {leg.get('final', 'Unavailable')}",
                "",
            ]
        )
    lines.extend(
        [
            f"External AI Leg Record: {wins}-{losses}",
            "Purpose: comparison and screenshot-bot learning only.",
        ]
    )
    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("record")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    payload = json.loads(Path(args.record).read_text(encoding="utf-8"))
    output = render(payload)
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
