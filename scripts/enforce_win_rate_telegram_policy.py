#!/usr/bin/env python3
"""Enforce Win-Rate-First price and sportsbook labels on Telegram text.

This is a presentation guardrail for the multi-sport paper messages. Diagnostic
records may still be stored for audit/training review, but prices outside the
approved Win-Rate-First range must never be presented as a BOT LEAN.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re

MIN_FAVORITE_AMERICAN = -400
MAX_UNDERDOG_AMERICAN = 300


def _csv(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().casefold() for item in value.split(",") if item.strip()}


def _american_odds_from_text(text: str) -> int | None:
    match = re.search(r"(?m)^Odds:\s*([+-]?\d+)\s*$", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def odds_in_win_rate_range(american_odds: int | None) -> bool:
    if american_odds is None:
        return False
    return MIN_FAVORITE_AMERICAN <= american_odds <= MAX_UNDERDOG_AMERICAN


def _label_book(text: str, accessible_bookmakers: set[str]) -> str:
    match = re.search(r"(?m)^Book:\s*(.+?)\s*$", text)
    if not match:
        return text
    book = match.group(1).strip()
    label = "Accessible Book" if book.casefold() in accessible_bookmakers else "Reference Book"
    return text[: match.start()] + f"{label}: {book}" + text[match.end() :]


def enforce_policy(text: str, *, accessible_bookmakers: set[str]) -> str:
    """Return Telegram text with fail-closed Win-Rate presentation rules."""
    has_lean = "BOT LEAN:" in text or "Bot Lean:" in text
    odds = _american_odds_from_text(text)
    in_range = odds_in_win_rate_range(odds)

    text = _label_book(text, accessible_bookmakers)

    if not has_lean:
        return text

    if not in_range:
        text = text.replace("🚨 BOT LEAN:", "⛔ OUTSIDE WIN-RATE RANGE:")
        text = text.replace("🚨 Bot Lean:", "⛔ Outside Win-Rate Range:")
        text = text.replace("Status: TEST CAPTURED", "Status: PASS — OUTSIDE WIN-RATE RANGE")
        text = text.replace("Status: READY", "Status: PASS — OUTSIDE WIN-RATE RANGE")
        note = "Win-Rate Lane: PASS — approved individual price range is -400 through +300"
        odds_line = re.search(r"(?m)^Odds:\s*[+-]?\d+\s*$", text)
        if odds_line and note not in text:
            insert_at = odds_line.end()
            text = text[:insert_at] + "\n" + note + text[insert_at:]

    return text


def process_file(path: Path, *, accessible_bookmakers: set[str]) -> None:
    if not path.exists():
        return
    original = path.read_text(encoding="utf-8")
    updated = enforce_policy(original, accessible_bookmakers=accessible_bookmakers)
    if updated != original:
        path.write_text(updated, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enforce Win-Rate Telegram display policy")
    parser.add_argument("paths", nargs="+")
    parser.add_argument(
        "--accessible-bookmakers",
        default=os.environ.get("ACCESSIBLE_BOOKMAKERS", ""),
        help="Comma-separated sportsbook keys available to the user",
    )
    args = parser.parse_args()

    accessible = _csv(args.accessible_bookmakers)
    for raw_path in args.paths:
        process_file(Path(raw_path), accessible_bookmakers=accessible)


if __name__ == "__main__":
    main()
