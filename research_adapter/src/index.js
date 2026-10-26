import { openMeteoObservations } from "./open_meteo.js";
import { officialEplObservations } from "./official_epl.js";
import { tennisAbstractObservations } from "./tennis_abstract.js";
import { officialUfcObservations } from "./official_ufc.js";
import {
  officialTennisAvailabilityObservations,
  officialTennisDrawObservations,
  officialTennisScheduleRestObservations,
  officialTennisStatusObservations
} from "./official_tennis.js";
import {
  OFFICIAL_TENNIS_AVAILABILITY_SOURCES,
  OFFICIAL_EPL_SOURCES,
  OFFICIAL_TENNIS_DRAW_SOURCES,
  OFFICIAL_TENNIS_SCHEDULE_REST_SOURCES,
  OFFICIAL_TENNIS_STATUS_SOURCES,
  OFFICIAL_UFC_SOURCES,
  SOURCE_DIRECTORY,
  TENNIS_ABSTRACT_SOURCES,
  WEATHER_SOURCES,
  publicManifest
} from "./sources.js";

const MAX_BODY_BYTES = 256 * 1024;
const MAX_EVENTS = 100;

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff"
    }
  });
}

function tokenFrom(request) {
  const authorization = request.headers.get("authorization") || "";
  return authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
}

function authorized(request, env) {
  const expected = String(env.MULTISPORT_RESEARCH_FEED_TOKEN || "");
  const actual = tokenFrom(request);
  if (!expected || !actual || expected.length !== actual.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ actual.charCodeAt(index);
  }
  return difference === 0;
}

function validateRequest(body, sourceKey, spec) {
  if (!body || body.contract_version !== 1 || body.test_only !== true) {
    throw new Error("invalid_contract");
  }
  if (body.source_key !== sourceKey || !Array.isArray(body.events) || body.events.length > MAX_EVENTS) {
    throw new Error("invalid_source_or_events");
  }
  if (!Number.isFinite(new Date(body.requested_at).getTime())) {
    throw new Error("invalid_requested_at");
  }
  for (const event of body.events) {
    if (!event || typeof event.event_id !== "string" || !event.event_id || !spec.sports.includes(event.sport)) {
      throw new Error("invalid_event_identity");
    }
    if (!Number.isFinite(new Date(event.commence_time).getTime()) || event.test_only !== true) {
      throw new Error("invalid_event_time_or_mode");
    }
  }
}

async function adapterResponse(sourceKey, spec, body, fetchImpl) {
  if (
    !OFFICIAL_EPL_SOURCES.has(sourceKey)
    && !WEATHER_SOURCES.has(sourceKey)
    && !OFFICIAL_TENNIS_AVAILABILITY_SOURCES.has(sourceKey)
    && !OFFICIAL_TENNIS_DRAW_SOURCES.has(sourceKey)
    && !OFFICIAL_TENNIS_SCHEDULE_REST_SOURCES.has(sourceKey)
    && !OFFICIAL_TENNIS_STATUS_SOURCES.has(sourceKey)
    && !OFFICIAL_UFC_SOURCES.has(sourceKey)
    && !TENNIS_ABSTRACT_SOURCES.has(sourceKey)
  ) {
    return {
      contract_version: 1,
      source_key: sourceKey,
      status: "unavailable",
      reason: "source_specific_connector_not_yet_verified",
      observations: [],
      unavailable: body.events.map((event) => ({
        event_id: event.event_id,
        reason: "source_specific_connector_not_yet_verified"
      })),
      test_only: true
    };
  }
  let result;
  if (OFFICIAL_EPL_SOURCES.has(sourceKey)) {
    result = await officialEplObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else if (OFFICIAL_UFC_SOURCES.has(sourceKey)) {
    result = await officialUfcObservations({
      sourceKey, spec, events: body.events, requestedAt: body.requested_at, fetchImpl
    });
  } else if (WEATHER_SOURCES.has(sourceKey)) {
    result = await openMeteoObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else if (OFFICIAL_TENNIS_DRAW_SOURCES.has(sourceKey)) {
    result = await officialTennisDrawObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else if (OFFICIAL_TENNIS_STATUS_SOURCES.has(sourceKey)) {
    result = await officialTennisStatusObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else if (OFFICIAL_TENNIS_AVAILABILITY_SOURCES.has(sourceKey)) {
    result = await officialTennisAvailabilityObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else if (TENNIS_ABSTRACT_SOURCES.has(sourceKey)) {
    result = await tennisAbstractObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  } else {
    result = await officialTennisScheduleRestObservations({
      sourceKey,
      spec,
      events: body.events,
      requestedAt: body.requested_at,
      fetchImpl
    });
  }
  return {
    contract_version: 1,
    source_key: sourceKey,
    status: result.observations.length ? "collected" : "unavailable",
    observations: result.observations,
    unavailable: result.unavailable,
    test_only: true
  };
}

export async function handleRequest(request, env, fetchImpl = fetch) {
  const url = new URL(request.url);
  if (request.method === "GET" && url.pathname === "/health") {
    return json({ status: "ok", service: "our-multisport-research-adapter", test_only: true });
  }
  if (request.method === "GET" && url.pathname === "/manifest") {
    return json({ contract_version: 1, sources: publicManifest(), test_only: true });
  }
  if (request.method === "GET" && url.pathname === "/configuration") {
    const feedUrls = Object.fromEntries(Object.keys(SOURCE_DIRECTORY).map((key) => [
      key,
      `${url.origin}/feeds/${key}`
    ]));
    return json({ MULTISPORT_RESEARCH_FEEDS_JSON: feedUrls, test_only: true });
  }
  const match = url.pathname.match(/^\/feeds\/([a-z0-9_]+)$/);
  if (!match) return json({ error: "not_found" }, 404);
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);
  const requestedSourceKey = match[1];
  const legacyAliases = {
    fbref: "premier_league_statistics",
    transfermarkt_changes: "premier_league_squad_changes"
  };
  const sourceKey = legacyAliases[requestedSourceKey] || requestedSourceKey;
  const spec = SOURCE_DIRECTORY[sourceKey];
  if (!spec) return json({ error: "unapproved_source" }, 404);
  if (!authorized(request, env)) return json({ error: "unauthorized" }, 401);
  const length = Number(request.headers.get("content-length") || 0);
  if (length > MAX_BODY_BYTES) return json({ error: "request_too_large" }, 413);
  try {
    const text = await request.text();
    if (new TextEncoder().encode(text).length > MAX_BODY_BYTES) {
      return json({ error: "request_too_large" }, 413);
    }
    const body = JSON.parse(text);
    validateRequest(body, sourceKey, spec);
    return json(await adapterResponse(sourceKey, spec, body, fetchImpl));
  } catch (error) {
    return json({ error: "invalid_request", reason: String(error?.message || "invalid_request") }, 400);
  }
}

export default {
  fetch(request, env) {
    return handleRequest(request, env);
  }
};
