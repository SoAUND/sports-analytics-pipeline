"""Official NFL source checks without guessing at unstable page markup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib

from .http import HttpTransport


OFFICIAL_PAGES = {
    "injuries": "https://www.nfl.com/injuries/",
    "transactions": "https://www.nfl.com/transactions/",
}


@dataclass(frozen=True)
class OfficialPageCheck:
    check_type: str
    source_url: str
    checked_at: datetime
    http_status: int
    content_sha256: str
    access_status: str
    warning: str | None = None


class OfficialNflCollector:
    """Confirm that required official pages are reachable and identifiable.

    NFL.com does not publish a stable public JSON contract for these pages.
    We therefore preserve the page check and require a separate confirmed data
    feed or manual review for player-level facts instead of silently scraping
    markup that may change.
    """

    provider_key = "nfl"

    def __init__(self, *, transport: HttpTransport):
        self._transport = transport

    def check(self, check_type: str) -> OfficialPageCheck:
        try:
            url = OFFICIAL_PAGES[check_type]
        except KeyError as exc:
            raise ValueError(f"Unsupported official NFL check: {check_type!r}") from exc
        response = self._transport.get(
            url,
            headers={"Accept": "text/html", "User-Agent": "SportsAnalyticsPipeline/0.2"},
        )
        text = response.body.decode("utf-8", errors="replace")
        marker = "injur" if check_type == "injuries" else "transaction"
        identifiable = "nfl" in text.casefold() and marker in text.casefold()
        return OfficialPageCheck(
            check_type=check_type,
            source_url=url,
            checked_at=response.received_at,
            http_status=response.status,
            content_sha256=hashlib.sha256(response.body).hexdigest(),
            access_status="confirmed" if identifiable else "unavailable",
            warning=None if identifiable else "Official page did not contain the expected marker",
        )

    def check_required_pages(self) -> tuple[OfficialPageCheck, ...]:
        return tuple(self.check(name) for name in ("injuries", "transactions"))
