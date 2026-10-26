const GEO_URL = "https://geocoding-api.open-meteo.com/v1/search";
const FORECAST_URL = "https://api.open-meteo.com/v1/forecast";

function normal(value) {
  return String(value || "").normalize("NFKD").replace(/[^\w\s-]/g, "").toLowerCase().trim();
}

function exactLocation(results, venue) {
  return (results || []).find((row) =>
    normal(row.name) === normal(venue.city) &&
    String(row.country_code || "").toUpperCase() === String(venue.country_code || "").toUpperCase()
  );
}

function closestHour(times, commenceTime) {
  const target = new Date(commenceTime).getTime();
  let best = -1;
  let distance = Number.POSITIVE_INFINITY;
  for (let index = 0; index < times.length; index += 1) {
    const candidate = new Date(`${times[index]}Z`).getTime();
    const currentDistance = Math.abs(candidate - target);
    if (Number.isFinite(candidate) && currentDistance < distance) {
      best = index;
      distance = currentDistance;
    }
  }
  return distance <= 60 * 60 * 1000 ? best : -1;
}

async function jsonFetch(url, fetchImpl) {
  const response = await fetchImpl(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`upstream_http_${response.status}`);
  return response.json();
}

export async function openMeteoObservations({ sourceKey, spec, events, requestedAt, fetchImpl = fetch }) {
  const observations = [];
  const unavailable = [];
  const geocodeCache = new Map();
  for (const event of events) {
    const venue = event.venue;
    if (!venue?.reviewed || !venue.city || !venue.country_code) {
      unavailable.push({ event_id: event.event_id, reason: "reviewed_venue_location_required" });
      continue;
    }
    try {
      const locationKey = `${normal(venue.city)}:${String(venue.country_code).toUpperCase()}`;
      let location = geocodeCache.get(locationKey);
      if (!location) {
        const geo = new URL(GEO_URL);
        geo.searchParams.set("name", venue.city);
        geo.searchParams.set("count", "10");
        geo.searchParams.set("language", "en");
        geo.searchParams.set("format", "json");
        const payload = await jsonFetch(geo, fetchImpl);
        location = exactLocation(payload.results, venue);
        if (!location) throw new Error("exact_venue_city_not_found");
        geocodeCache.set(locationKey, location);
      }

      const start = String(event.commence_time).slice(0, 10);
      const forecastUrl = new URL(FORECAST_URL);
      forecastUrl.searchParams.set("latitude", String(location.latitude));
      forecastUrl.searchParams.set("longitude", String(location.longitude));
      forecastUrl.searchParams.set("hourly", "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m,wind_gusts_10m");
      forecastUrl.searchParams.set("timezone", "UTC");
      forecastUrl.searchParams.set("start_date", start);
      forecastUrl.searchParams.set("end_date", start);
      const forecast = await jsonFetch(forecastUrl, fetchImpl);
      const hourly = forecast.hourly || {};
      const index = closestHour(Array.isArray(hourly.time) ? hourly.time : [], event.commence_time);
      if (index < 0) throw new Error("event_hour_not_in_forecast");
      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        source_updated_at: requestedAt,
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          venue_id: venue.venue_id,
          venue_name: venue.venue_name,
          location: {
            city: venue.city,
            country_code: venue.country_code,
            latitude: location.latitude,
            longitude: location.longitude
          },
          forecast_time: hourly.time[index],
          temperature_2m: hourly.temperature_2m?.[index] ?? null,
          precipitation_probability: hourly.precipitation_probability?.[index] ?? null,
          precipitation: hourly.precipitation?.[index] ?? null,
          weather_code: hourly.weather_code?.[index] ?? null,
          wind_speed_10m: hourly.wind_speed_10m?.[index] ?? null,
          wind_gusts_10m: hourly.wind_gusts_10m?.[index] ?? null,
          units: forecast.hourly_units || {},
          material_change: false,
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({ event_id: event.event_id, reason: String(error?.message || "weather_unavailable") });
    }
  }
  return { observations, unavailable };
}
