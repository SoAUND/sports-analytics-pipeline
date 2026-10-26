"""National Weather Service forecast collection for weather-exposed U.S. games."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from .http import HttpTransport
from .the_odds_api import parse_timestamp


BASE_URL = "https://api.weather.gov"


@dataclass(frozen=True)
class WeatherRecord:
    forecast_for: datetime
    temperature_f: float | None
    sustained_wind_mph: float | None
    wind_gust_mph: float | None
    precipitation_probability: float | None
    precipitation_type: str | None
    alerts: tuple[dict[str, Any], ...]
    source_updated_at: datetime
    collected_at: datetime


@dataclass(frozen=True)
class WeatherBatch:
    provider_key: str
    latitude: float
    longitude: float
    raw_payload: dict[str, Any]
    record: WeatherRecord
    warnings: tuple[str, ...] = ()


def _wind_values(wind_speed: str | None, detailed: str | None) -> tuple[float | None, float | None]:
    numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", wind_speed or "")]
    sustained = numbers[0] if numbers else None
    gust_match = re.search(r"gusts? (?:as high as|up to|to) (\d+(?:\.\d+)?) mph", detailed or "", re.I)
    gust = float(gust_match.group(1)) if gust_match else None
    return sustained, gust


def _precipitation_type(short_forecast: str | None) -> str | None:
    text = (short_forecast or "").casefold()
    found = [
        name
        for name in ("rain", "snow", "sleet", "freezing rain", "thunderstorms")
        if name in text
    ]
    return ", ".join(found) if found else None


def _probability(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not 0 <= number <= 100:
        raise ValueError("NWS precipitation probability is outside 0 to 100")
    return number / 100.0


def parse_weather_payloads(
    *,
    latitude: float,
    longitude: float,
    kickoff_time: datetime,
    points_payload: dict[str, Any],
    hourly_payload: dict[str, Any],
    alerts_payload: dict[str, Any],
    collected_at: datetime,
) -> WeatherBatch:
    if kickoff_time.tzinfo is None or kickoff_time.utcoffset() is None:
        raise ValueError("kickoff_time must include a time zone")
    kickoff = kickoff_time.astimezone(timezone.utc)
    periods = hourly_payload.get("properties", {}).get("periods", [])
    selected: dict[str, Any] | None = None
    for period in periods:
        start = parse_timestamp(str(period["startTime"]))
        end = parse_timestamp(str(period["endTime"]))
        if start <= kickoff < end:
            selected = period
            break
    if selected is None:
        raise ValueError("NWS hourly forecast does not cover the scheduled kickoff")

    sustained, gust = _wind_values(
        selected.get("windSpeed"), selected.get("detailedForecast")
    )
    probability_value = (selected.get("probabilityOfPrecipitation") or {}).get("value")
    alerts: list[dict[str, Any]] = []
    for feature in alerts_payload.get("features", []):
        properties = feature.get("properties", {})
        alerts.append(
            {
                "id": feature.get("id"),
                "event": properties.get("event"),
                "severity": properties.get("severity"),
                "urgency": properties.get("urgency"),
                "onset": properties.get("onset"),
                "ends": properties.get("ends"),
                "headline": properties.get("headline"),
            }
        )

    properties = hourly_payload.get("properties", {})
    updated_value = properties.get("updated") or properties.get("generatedAt")
    if not updated_value:
        raise ValueError("NWS forecast is missing its update timestamp")
    source_updated = parse_timestamp(str(updated_value))
    if source_updated > collected_at + timedelta(minutes=5):
        raise ValueError("NWS update time materially follows collection time")

    temperature = selected.get("temperature")
    unit = str(selected.get("temperatureUnit", "F")).upper()
    if temperature is not None and unit == "C":
        temperature = (float(temperature) * 9 / 5) + 32
    elif temperature is not None:
        temperature = float(temperature)

    return WeatherBatch(
        provider_key="nws",
        latitude=latitude,
        longitude=longitude,
        raw_payload={
            "points": points_payload,
            "hourly": hourly_payload,
            "alerts": alerts_payload,
        },
        record=WeatherRecord(
            forecast_for=parse_timestamp(str(selected["startTime"])),
            temperature_f=temperature,
            sustained_wind_mph=sustained,
            wind_gust_mph=gust,
            precipitation_probability=_probability(probability_value),
            precipitation_type=_precipitation_type(selected.get("shortForecast")),
            alerts=tuple(alerts),
            source_updated_at=source_updated,
            collected_at=collected_at,
        ),
    )


class NwsCollector:
    provider_key = "nws"

    def __init__(self, *, transport: HttpTransport, user_agent: str):
        if not user_agent.strip() or "replace-with-real-contact" in user_agent.casefold():
            raise ValueError("A real identifying NWS User-Agent is required")
        self._transport = transport
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/geo+json",
        }

    def collect_forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        kickoff_time: datetime,
    ) -> WeatherBatch:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("Invalid stadium coordinates")
        points = self._transport.get(
            f"{BASE_URL}/points/{latitude:.4f},{longitude:.4f}",
            headers=self._headers,
        )
        points_payload = points.json()
        forecast_url = points_payload.get("properties", {}).get("forecastHourly")
        if not forecast_url or not str(forecast_url).startswith(BASE_URL):
            raise ValueError("NWS points response is missing a trusted hourly forecast URL")
        hourly = self._transport.get(str(forecast_url), headers=self._headers)
        alerts = self._transport.get(
            f"{BASE_URL}/alerts/active",
            params={"point": f"{latitude:.4f},{longitude:.4f}"},
            headers=self._headers,
        )
        collected_at = max(points.received_at, hourly.received_at, alerts.received_at)
        return parse_weather_payloads(
            latitude=latitude,
            longitude=longitude,
            kickoff_time=kickoff_time,
            points_payload=points_payload,
            hourly_payload=hourly.json(),
            alerts_payload=alerts.json(),
            collected_at=collected_at,
        )
