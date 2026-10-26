"""Protected, read-only clients and routing metadata for our seven API services.

Connection probes intentionally request only small catalog, status, or account-usage
resources. They never return raw responses or credentials. Backup odds providers are
registered for observation, but are not eligible to feed decisions until a separate
normalization and bookmaker-mapping review is complete.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from ..config import REQUIRED_PROVIDER_KEYS, Settings
from .http import HttpResponse, HttpTransport


@dataclass(frozen=True)
class ProviderProbe:
    provider_key: str
    purpose: str
    configured: bool
    reachable: bool | None
    endpoint: str
    http_status: int | None = None
    response_shape: str | None = None
    error: str | None = None

    def safe_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderRoute:
    data_kind: str
    sports: tuple[str, ...]
    primary: str
    fallbacks: tuple[str, ...]
    automatic_fallback: bool
    note: str


PROVIDER_PURPOSES = {
    "the_odds_api": "Primary current odds and scores",
    "oddspapi": "Backup odds and historical-line research",
    "odds_api_io": "Backup odds and historical-line research",
    "sports_game_odds": "Backup odds, results, and usage monitoring",
    "api_sports": "Cross-sport schedules, results, and statistics",
    "cfbd": "College-football historical and game data",
    "football_data": "Premier League schedules, results, and standings",
}

PROVIDER_ROUTES = (
    ProviderRoute(
        data_kind="market_odds",
        sports=("nfl", "mlb", "nba", "wnba", "epl", "mma", "atp", "wta"),
        primary="the_odds_api",
        fallbacks=("oddspapi", "odds_api_io", "sports_game_odds"),
        automatic_fallback=False,
        note=(
            "Backups remain observation-only until their event, market, participant, "
            "and bookmaker mappings pass independent normalization tests."
        ),
    ),
    ProviderRoute(
        data_kind="league_data",
        sports=("nba", "wnba", "mma"),
        primary="api_sports",
        fallbacks=(),
        automatic_fallback=False,
        note="Enrichment only; it cannot replace the approved odds source.",
    ),
    ProviderRoute(
        data_kind="college_football_data",
        sports=("cfb",),
        primary="cfbd",
        fallbacks=("api_sports",),
        automatic_fallback=False,
        note="College-football-only enrichment route.",
    ),
    ProviderRoute(
        data_kind="premier_league_data",
        sports=("epl",),
        primary="football_data",
        fallbacks=("api_sports",),
        automatic_fallback=False,
        note="Schedule, result, and standings verification route.",
    ),
)


def _shape(payload: Any) -> str:
    if isinstance(payload, list):
        return "list"
    if isinstance(payload, dict):
        return "object"
    raise ValueError("Provider returned an unsupported JSON response shape")


class _ReadOnlyProviderClient:
    provider_key = ""
    endpoint = ""

    def __init__(self, *, api_key: str, transport: HttpTransport):
        if not api_key.strip():
            raise ValueError(f"A non-empty {self.provider_key} API key is required")
        self._api_key = api_key
        self._transport = transport

    def _get(
        self,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        return self._transport.get(self.endpoint, params=params, headers=headers)

    def probe(self) -> ProviderProbe:
        response = self._request()
        response_shape = _shape(response.json())
        return ProviderProbe(
            provider_key=self.provider_key,
            purpose=PROVIDER_PURPOSES[self.provider_key],
            configured=True,
            reachable=True,
            endpoint=self.endpoint,
            http_status=response.status,
            response_shape=response_shape,
        )

    def _request(self) -> HttpResponse:
        raise NotImplementedError


class TheOddsApiClient(_ReadOnlyProviderClient):
    provider_key = "the_odds_api"
    endpoint = "https://api.the-odds-api.com/v4/sports/"

    def _request(self) -> HttpResponse:
        return self._get(params={"apiKey": self._api_key})


class OddsPapiClient(_ReadOnlyProviderClient):
    provider_key = "oddspapi"
    endpoint = "https://v5.oddspapi.io/en/sports"

    def _request(self) -> HttpResponse:
        return self._get(params={"apiKey": self._api_key})


class OddsApiIoClient(_ReadOnlyProviderClient):
    provider_key = "odds_api_io"
    endpoint = "https://api.odds-api.io/v3/bookmakers/selected"

    def _request(self) -> HttpResponse:
        return self._get(params={"apiKey": self._api_key})


class SportsGameOddsClient(_ReadOnlyProviderClient):
    provider_key = "sports_game_odds"
    endpoint = "https://api.sportsgameodds.com/v2/account/usage/"

    def _request(self) -> HttpResponse:
        return self._get(headers={"x-api-key": self._api_key})


class ApiSportsClient(_ReadOnlyProviderClient):
    provider_key = "api_sports"
    endpoint = "https://v3.football.api-sports.io/status"

    def _request(self) -> HttpResponse:
        return self._get(headers={"x-apisports-key": self._api_key})


class CollegeFootballDataClient(_ReadOnlyProviderClient):
    provider_key = "cfbd"
    endpoint = "https://api.collegefootballdata.com/games"

    def _request(self) -> HttpResponse:
        return self._get(
            params={"year": "2023", "team": "Michigan"},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )


class FootballDataClient(_ReadOnlyProviderClient):
    provider_key = "football_data"
    endpoint = "https://api.football-data.org/v4/competitions/PL"

    def _request(self) -> HttpResponse:
        return self._get(headers={"X-Auth-Token": self._api_key})


CLIENTS: dict[str, type[_ReadOnlyProviderClient]] = {
    client.provider_key: client
    for client in (
        TheOddsApiClient,
        OddsPapiClient,
        OddsApiIoClient,
        SportsGameOddsClient,
        ApiSportsClient,
        CollegeFootballDataClient,
        FootballDataClient,
    )
}


def configuration_report(settings: Settings) -> dict[str, ProviderProbe]:
    keys = settings.provider_api_keys()
    return {
        provider_key: ProviderProbe(
            provider_key=provider_key,
            purpose=PROVIDER_PURPOSES[provider_key],
            configured=bool(keys[provider_key]),
            reachable=None,
            endpoint=CLIENTS[provider_key].endpoint,
        )
        for provider_key in REQUIRED_PROVIDER_KEYS
    }


def live_probe_report(
    settings: Settings,
    *,
    transport: HttpTransport,
    provider_keys: tuple[str, ...] = REQUIRED_PROVIDER_KEYS,
) -> dict[str, ProviderProbe]:
    keys = settings.provider_api_keys()
    unknown = tuple(key for key in provider_keys if key not in CLIENTS)
    if unknown:
        raise ValueError(f"Unknown provider keys: {', '.join(unknown)}")
    report: dict[str, ProviderProbe] = {}
    for provider_key in provider_keys:
        key = keys[provider_key]
        if not key:
            report[provider_key] = ProviderProbe(
                provider_key=provider_key,
                purpose=PROVIDER_PURPOSES[provider_key],
                configured=False,
                reachable=None,
                endpoint=CLIENTS[provider_key].endpoint,
                error="API key is not configured",
            )
            continue
        try:
            report[provider_key] = CLIENTS[provider_key](
                api_key=key,
                transport=transport,
            ).probe()
        except Exception as exc:
            safe_error = str(exc).replace(key, "[REDACTED]")
            report[provider_key] = ProviderProbe(
                provider_key=provider_key,
                purpose=PROVIDER_PURPOSES[provider_key],
                configured=True,
                reachable=False,
                endpoint=CLIENTS[provider_key].endpoint,
                error=safe_error,
            )
    return report


def safe_provider_report(
    probes: Mapping[str, ProviderProbe],
) -> dict[str, dict[str, object]]:
    return {key: probe.safe_dict() for key, probe in probes.items()}


def routing_report(settings: Settings) -> tuple[dict[str, object], ...]:
    configured = settings.provider_api_keys()
    return tuple(
        {
            "data_kind": route.data_kind,
            "sports": route.sports,
            "primary": route.primary,
            "primary_configured": bool(configured[route.primary]),
            "fallbacks": tuple(
                {"provider": key, "configured": bool(configured[key])}
                for key in route.fallbacks
            ),
            "automatic_fallback": route.automatic_fallback,
            "note": route.note,
        }
        for route in PROVIDER_ROUTES
    )
