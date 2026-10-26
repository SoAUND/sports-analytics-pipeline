#!/usr/bin/env python3
"""Fail closed unless the configured Telegram token belongs to the expected bot."""

from __future__ import annotations

import json
import os
import urllib.request


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    expected = os.environ.get("TELEGRAM_EXPECTED_USERNAME", "").strip().lstrip("@")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not configured")
    if not expected:
        raise SystemExit("TELEGRAM_EXPECTED_USERNAME is not configured")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getMe",
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not payload.get("ok") or not isinstance(payload.get("result"), dict):
        raise SystemExit("Telegram getMe verification failed")

    actual = str(payload["result"].get("username") or "").strip().lstrip("@")
    if actual.casefold() != expected.casefold():
        raise SystemExit(
            f"Telegram bot identity mismatch: expected @{expected}, got @{actual or 'unknown'}"
        )

    print(f"Telegram bot identity verified: @{actual}")


if __name__ == "__main__":
    main()
