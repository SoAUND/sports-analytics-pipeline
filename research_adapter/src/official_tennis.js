const MAX_OFFICIAL_PAGE_BYTES = 2 * 1024 * 1024;

const TOURNAMENT_DIRECTORY = Object.freeze({
  tennis_atp_cincinnati_open: {
    sport: "atp",
    drawUrl: "https://www.atptour.com/en/scores/current/cincinnati/422/draws",
    host: "www.atptour.com",
    tournament: "Cincinnati Open",
    segmentBoundary: /\bH2H(?:\s+Stats)?\b/giu
  },
  tennis_atp_us_open: {
    sport: "atp",
    drawUrl: "https://www.atptour.com/en/scores/current/us-open/560/draws",
    host: "www.atptour.com",
    tournament: "US Open",
    segmentBoundary: /\bH2H(?:\s+Stats)?\b/giu
  },
  tennis_wta_cincinnati_open: {
    sport: "wta",
    drawUrl: "https://www.wtatennis.com/tournaments/cincinnati-open/draws",
    statusUrl: "https://www.wtatennis.com/tournaments/cincinnati-open/draws",
    host: "www.wtatennis.com",
    tournament: "Cincinnati Open",
    segmentBoundary: /\bUpcoming\s+Suspended\s+Finished\b/giu
  },
  tennis_wta_us_open: {
    sport: "wta",
    drawUrl: "https://www.wtatennis.com/tournaments/905/us-open/{year}/draws",
    statusUrl: "https://www.wtatennis.com/tournaments/905/us-open/{year}/draws",
    host: "www.wtatennis.com",
    tournament: "US Open",
    segmentBoundary: /\bUpcoming\s+Suspended\s+Finished\b/giu
  },
  tennis_wta_monterrey_open: {
    sport: "wta",
    drawUrl: "https://www.wtatennis.com/tournaments/1039/monterrey/{year}/draws",
    statusUrl: "https://www.wtatennis.com/tournaments/1039/monterrey/{year}/draws",
    host: "www.wtatennis.com",
    tournament: "Abierto GNP Seguros",
    segmentBoundary: /\bUpcoming\s+Suspended\s+Finished\b/giu,
    schedule: {
      scoreUrl: "https://www.wtatennis.com/tournaments/1039/monterrey/{year}/scores/{match_code}",
      location: {
        label: "MONTERREY, MEX",
        city: "Monterrey",
        countryCode: "MX",
        latitude: 25.6866,
        longitude: -100.3161,
        timeZone: "America/Monterrey"
      },
      previousTournaments: [
        {
          sport: "wta",
          drawUrl: "https://www.wtatennis.com/tournaments/1017/cincinnati/{year}/draws",
          scoreUrl: "https://www.wtatennis.com/tournaments/1017/cincinnati/{year}/scores/{match_code}",
          host: "www.wtatennis.com",
          tournament: "Cincinnati Open",
          location: {
            label: "CINCINNATI, USA",
            latitude: 39.1031,
            longitude: -84.512,
            timeZone: "America/New_York"
          }
        },
        {
          sport: "wta",
          drawUrl: "https://www.wtatennis.com/tournaments/2042/hamburg/{year}/draws",
          scoreUrl: "https://www.wtatennis.com/tournaments/2042/hamburg/{year}/scores/{match_code}",
          host: "www.wtatennis.com",
          tournament: "MSC Hamburg Ladies Open",
          location: {
            label: "HAMBURG, GER",
            latitude: 53.5511,
            longitude: 9.9937,
            timeZone: "Europe/Berlin"
          }
        }
      ]
    }
  }
});

function tournamentUrl(template, year) {
  return String(template || "").replace("{year}", String(year));
}

function scoreUrl(template, year, matchCode) {
  return tournamentUrl(template, year).replace("{match_code}", encodeURIComponent(matchCode));
}

function decodeEntities(value) {
  const named = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"'
  };
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/giu, (match, entity) => {
    const key = String(entity).toLowerCase();
    if (key.startsWith("#x")) return String.fromCodePoint(Number.parseInt(key.slice(2), 16));
    if (key.startsWith("#")) return String.fromCodePoint(Number.parseInt(key.slice(1), 10));
    return named[key] ?? match;
  });
}

function visibleText(html) {
  return decodeEntities(String(html || ""))
    .replace(/<(script|style|svg)\b[^>]*>[\s\S]*?<\/\1>/giu, " ")
    .replace(/<\/?(?:a|article|br|div|h[1-6]|li|p|section|td|tr)\b[^>]*>/giu, "\n")
    .replace(/<[^>]+>/gu, " ")
    .split(/\r?\n/u)
    .map((line) => line.replace(/\s+/gu, " ").trim())
    .filter(Boolean)
    .join("\n");
}

function normal(value) {
  return String(value || "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/gu, "")
    .replace(/[^a-z0-9]+/giu, " ")
    .toLowerCase()
    .replace(/\s+/gu, " ")
    .trim();
}

function identity(name) {
  const tokens = normal(name).split(" ").filter(Boolean);
  if (tokens.length < 2) return null;
  const suffixes = new Set(["ii", "iii", "iv", "jr", "sr"]);
  while (tokens.length > 2 && suffixes.has(tokens.at(-1))) tokens.pop();
  return { firstInitial: tokens[0][0], surname: tokens.at(-1) };
}

function officialLabel(segment, participantName) {
  const expected = identity(participantName);
  if (!expected) return null;
  const matches = segment.split("\n").filter((line) => {
    const tokens = normal(line).split(" ").filter(Boolean);
    if (tokens.length < 2 || tokens.length > 8) return false;
    return tokens[0][0] === expected.firstInitial && tokens.includes(expected.surname);
  });
  return matches.length === 1 ? matches[0] : null;
}

function exactPair(text, event, boundary) {
  const homeName = String(event.home?.name || "").trim();
  const awayName = String(event.away?.name || "").trim();
  if (!identity(homeName) || !identity(awayName) || normal(homeName) === normal(awayName)) {
    throw new Error("participant_identity_required");
  }
  const candidates = text.split(boundary).map((segment) => {
    const homeLabel = officialLabel(segment, homeName);
    const awayLabel = officialLabel(segment, awayName);
    return homeLabel && awayLabel && normal(homeLabel) !== normal(awayLabel)
      ? { homeLabel, awayLabel, segment }
      : null;
  }).filter(Boolean);
  if (candidates.length === 0) throw new Error("exact_official_pair_not_found");
  if (candidates.length > 1) throw new Error("ambiguous_official_pair");
  return candidates[0];
}

async function officialPage(config, url, year, fetchImpl) {
  const response = await fetchImpl(url, {
    headers: { Accept: "text/html,application/xhtml+xml" },
    redirect: "follow"
  });
  if (!response.ok) throw new Error(`upstream_http_${response.status}`);
  const contentType = String(response.headers.get("content-type") || "").toLowerCase();
  if (!contentType.includes("text/html")) throw new Error("official_draw_content_type");
  const contentLength = Number(response.headers.get("content-length") || 0);
  if (contentLength > MAX_OFFICIAL_PAGE_BYTES) throw new Error("official_draw_page_too_large");
  if (response.url && new URL(response.url).hostname !== config.host) {
    throw new Error("official_draw_redirect_rejected");
  }
  const html = await response.text();
  if (new TextEncoder().encode(html).length > MAX_OFFICIAL_PAGE_BYTES) {
    throw new Error("official_draw_page_too_large");
  }
  const text = visibleText(html);
  if (!normal(text).includes(normal(config.tournament)) || !text.includes(String(year))) {
    throw new Error("official_tournament_identity_not_found");
  }
  return { html, text };
}

function wtaMatchBlocks(html) {
  const pattern = /<div\b(?=[^>]*\bclass="[^"]*\btennis-match\b[^"]*")(?=[^>]*\bdata-status="([^"]+)")[^>]*>([\s\S]*?)<\/table>/giu;
  return [...String(html || "").matchAll(pattern)].map((match) => {
    const identifier = match[0].match(/\bjs-match-(\d+)-(\d{4})-([a-z0-9]+)\b/iu);
    return {
      statusCode: String(match[1] || "").trim().toUpperCase(),
      tournamentId: identifier?.[1] || null,
      year: identifier ? Number(identifier[2]) : null,
      matchCode: identifier?.[3]?.toUpperCase() || null,
      html: match[0]
    };
  });
}

function exactWtaStatus(html, event) {
  const homeName = String(event.home?.name || "").trim();
  const awayName = String(event.away?.name || "").trim();
  if (!identity(homeName) || !identity(awayName) || normal(homeName) === normal(awayName)) {
    throw new Error("participant_identity_required");
  }
  const candidates = wtaMatchBlocks(html).map((block) => {
    const text = visibleText(block.html);
    const homeLabel = officialLabel(text, homeName);
    const awayLabel = officialLabel(text, awayName);
    return homeLabel && awayLabel && normal(homeLabel) !== normal(awayLabel)
      ? { ...block, homeLabel, awayLabel }
      : null;
  }).filter(Boolean);
  if (candidates.length === 0) throw new Error("exact_official_pair_not_found");
  if (candidates.length > 1) throw new Error("ambiguous_official_pair");
  const status = { U: "scheduled", F: "final" }[candidates[0].statusCode];
  if (!status) throw new Error("unreviewed_official_status_code");
  return { ...candidates[0], status };
}

function officialUnavailabilitySignal(matchHtml) {
  const text = visibleText(matchHtml);
  const signal = text.match(
    /\b(?:withdrawn|withdrawal|walkover|retired|retirement|defaulted|cancelled|canceled)\b|\bW\s*\/\s*O\b/iu
  );
  return signal ? normal(signal[0]).replace(/\s+/gu, "_") : null;
}

const MONTHS = Object.freeze({
  jan: 1,
  feb: 2,
  mar: 3,
  apr: 4,
  may: 5,
  jun: 6,
  jul: 7,
  aug: 8,
  sep: 9,
  oct: 10,
  nov: 11,
  dec: 12
});

function matchInfo(text) {
  const lines = String(text || "").split("\n").map((line) => line.trim()).filter(Boolean);
  const start = lines.findIndex((line) => normal(line) === "match info");
  if (start < 0) throw new Error("official_match_info_not_found");
  const field = (label) => {
    const wanted = normal(label);
    const index = lines.findIndex((line, lineIndex) => lineIndex > start && normal(line) === wanted);
    return index >= 0 ? lines[index + 1] || null : null;
  };
  const startTime = field("Start Time");
  const location = field("Location");
  if (!startTime || !location) throw new Error("official_match_schedule_incomplete");
  return { startTime, location };
}

function officialDate(value) {
  const match = String(value || "").trim().match(
    /^(?:(?:mon|tue|wed|thu|fri|sat|sun)\s+)?(\d{1,2})\s+([a-z]{3})\s+(\d{4})$/iu
  );
  if (!match) throw new Error("official_match_date_unreviewed");
  const month = MONTHS[match[2].toLowerCase()];
  const day = Number(match[1]);
  const year = Number(match[3]);
  if (!month || day < 1 || day > 31) throw new Error("official_match_date_unreviewed");
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function localDate(instant, timeZone) {
  const date = new Date(instant);
  if (!Number.isFinite(date.getTime())) throw new Error("invalid_event_time");
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(date);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function calendarDays(fromDate, toDate) {
  const from = Date.parse(`${fromDate}T00:00:00Z`);
  const to = Date.parse(`${toDate}T00:00:00Z`);
  const days = Math.round((to - from) / 86_400_000);
  if (!Number.isInteger(days)) throw new Error("official_match_date_unreviewed");
  return days;
}

function identityPresent(text, participantName) {
  const expected = identity(participantName);
  if (!expected) return false;
  return String(text || "").split("\n").some((line) => {
    const tokens = normal(line).split(" ").filter(Boolean);
    return tokens.length >= 2
      && tokens[0][0] === expected.firstInitial
      && tokens.includes(expected.surname);
  });
}

function sameLocation(actual, expected) {
  return normal(actual) === normal(expected);
}

function distanceKm(from, to) {
  const radians = (degrees) => degrees * Math.PI / 180;
  const latitudeDelta = radians(to.latitude - from.latitude);
  const longitudeDelta = radians(to.longitude - from.longitude);
  const a = Math.sin(latitudeDelta / 2) ** 2
    + Math.cos(radians(from.latitude)) * Math.cos(radians(to.latitude))
    * Math.sin(longitudeDelta / 2) ** 2;
  return Math.round(6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a)));
}

function timeZoneOffsetMinutes(dateValue, timeZone) {
  const instant = new Date(`${dateValue}T12:00:00Z`);
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  }).formatToParts(instant);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const represented = Date.UTC(
    Number(value.year),
    Number(value.month) - 1,
    Number(value.day),
    Number(value.hour) % 24,
    Number(value.minute),
    Number(value.second)
  );
  return Math.round((represented - instant.getTime()) / 60_000);
}

async function cachedOfficialPage(cache, config, url, year, fetchImpl) {
  const key = `${url}:${year}`;
  let page = cache.get(key);
  if (!page) {
    page = await officialPage(config, url, year, fetchImpl);
    cache.set(key, page);
  }
  return page;
}

function completedMatchesForParticipant(html, participantName, excludedMatchCode = null) {
  return wtaMatchBlocks(html).filter((block) => (
    block.statusCode === "F"
    && block.matchCode
    && block.matchCode !== excludedMatchCode
    && officialLabel(visibleText(block.html), participantName)
  ));
}

async function latestPreviousMatch({
  participant,
  currentDate,
  currentYear,
  currentMatchCode,
  currentTournament,
  previousTournaments,
  pageCache,
  fetchImpl
}) {
  const tournaments = [currentTournament, ...previousTournaments];
  const candidates = [];
  for (const tournament of tournaments) {
    const drawUrl = tournamentUrl(tournament.drawUrl, currentYear);
    const drawPage = await cachedOfficialPage(pageCache, tournament, drawUrl, currentYear, fetchImpl);
    const matches = completedMatchesForParticipant(
      drawPage.html,
      participant.name,
      tournament === currentTournament ? currentMatchCode : null
    );
    for (const match of matches) {
      const detailUrl = scoreUrl(tournament.scoreUrl, currentYear, match.matchCode);
      const detailPage = await cachedOfficialPage(pageCache, tournament, detailUrl, currentYear, fetchImpl);
      if (!identityPresent(detailPage.text, participant.name)) continue;
      const info = matchInfo(detailPage.text);
      // Prior location is anchored by the reviewed tournament configuration and exact score URL.
      const date = officialDate(info.startTime);
      if (calendarDays(date, currentDate) < 0) continue;
      candidates.push({
        date,
        detailUrl,
        matchCode: match.matchCode,
        tournament: tournament.tournament,
        location: tournament.location
      });
    }
  }
  if (candidates.length === 0) throw new Error(`official_previous_match_not_found:${participant.name}`);
  candidates.sort((left, right) => right.date.localeCompare(left.date));
  if (candidates[1]?.date === candidates[0].date) {
    throw new Error(`ambiguous_official_previous_match:${participant.name}`);
  }
  return candidates[0];
}

export async function officialTennisDrawObservations({
  sourceKey,
  spec,
  events,
  requestedAt,
  fetchImpl = fetch
}) {
  const observations = [];
  const unavailable = [];
  const pageCache = new Map();
  for (const event of events) {
    const config = TOURNAMENT_DIRECTORY[event.provider_sport_key];
    if (!config || config.sport !== event.sport || spec.category !== "lineup_or_participants") {
      unavailable.push({ event_id: event.event_id, reason: "unsupported_tournament_provider_key" });
      continue;
    }
    try {
      const year = new Date(event.commence_time).getUTCFullYear();
      const drawUrl = tournamentUrl(config.drawUrl, year);
      const cacheKey = `${drawUrl}:${year}`;
      let page = pageCache.get(cacheKey);
      if (!page) {
        page = await officialPage(config, drawUrl, year, fetchImpl);
        pageCache.set(cacheKey, page);
      }
      const pair = exactPair(page.text, event, config.segmentBoundary);
      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          provider_sport_key: event.provider_sport_key,
          source_event_id: event.source_event_id,
          tournament: config.tournament,
          official_url: drawUrl,
          participants: [
            {
              participant_id: event.home.participant_id,
              source_name: event.home.name,
              official_label: pair.homeLabel
            },
            {
              participant_id: event.away.participant_id,
              source_name: event.away.name,
              official_label: pair.awayLabel
            }
          ],
          match_type: "singles",
          material_change: false,
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({
        event_id: event.event_id,
        reason: String(error?.message || "official_draw_unavailable")
      });
    }
  }
  return { observations, unavailable };
}

export async function officialTennisStatusObservations({
  sourceKey,
  spec,
  events,
  requestedAt,
  fetchImpl = fetch
}) {
  const observations = [];
  const unavailable = [];
  const pageCache = new Map();
  for (const event of events) {
    const config = TOURNAMENT_DIRECTORY[event.provider_sport_key];
    if (
      !config
      || config.sport !== "wta"
      || event.sport !== "wta"
      || !config.statusUrl
      || spec.category !== "official_status"
    ) {
      unavailable.push({ event_id: event.event_id, reason: "unsupported_tournament_provider_key" });
      continue;
    }
    try {
      const year = new Date(event.commence_time).getUTCFullYear();
      const statusUrl = tournamentUrl(config.statusUrl, year);
      const cacheKey = `${statusUrl}:${year}`;
      let page = pageCache.get(cacheKey);
      if (!page) {
        page = await officialPage(config, statusUrl, year, fetchImpl);
        pageCache.set(cacheKey, page);
      }
      const match = exactWtaStatus(page.html, event);
      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          provider_sport_key: event.provider_sport_key,
          source_event_id: event.source_event_id,
          tournament: config.tournament,
          official_url: statusUrl,
          game_status: match.status,
          official_status_code: match.statusCode || null,
          official_status_label: match.label || null,
          participants: [
            {
              participant_id: event.home.participant_id,
              source_name: event.home.name,
              official_label: match.homeLabel
            },
            {
              participant_id: event.away.participant_id,
              source_name: event.away.name,
              official_label: match.awayLabel
            }
          ],
          material_change: match.status !== "scheduled",
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({
        event_id: event.event_id,
        reason: String(error?.message || "official_status_unavailable")
      });
    }
  }
  return { observations, unavailable };
}

export async function officialTennisAvailabilityObservations({
  sourceKey,
  spec,
  events,
  requestedAt,
  fetchImpl = fetch
}) {
  const observations = [];
  const unavailable = [];
  const pageCache = new Map();
  for (const event of events) {
    const config = TOURNAMENT_DIRECTORY[event.provider_sport_key];
    if (
      !config
      || config.sport !== "wta"
      || event.sport !== "wta"
      || !config.statusUrl
      || spec.category !== "injury_availability"
    ) {
      unavailable.push({ event_id: event.event_id, reason: "unsupported_tournament_provider_key" });
      continue;
    }
    try {
      const year = new Date(event.commence_time).getUTCFullYear();
      const statusUrl = tournamentUrl(config.statusUrl, year);
      const cacheKey = `${statusUrl}:${year}`;
      let page = pageCache.get(cacheKey);
      if (!page) {
        page = await officialPage(config, statusUrl, year, fetchImpl);
        pageCache.set(cacheKey, page);
      }
      const match = exactWtaStatus(page.html, event);
      if (match.status !== "scheduled") throw new Error("official_active_match_required");
      const signal = officialUnavailabilitySignal(match.html);
      if (signal) throw new Error(`official_unavailability_signal:${signal}`);
      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          provider_sport_key: event.provider_sport_key,
          source_event_id: event.source_event_id,
          tournament: config.tournament,
          official_url: statusUrl,
          availability_basis: "official_active_scheduled_match",
          official_status_code: match.statusCode,
          participants: [
            {
              participant_id: event.home.participant_id,
              source_name: event.home.name,
              official_label: match.homeLabel,
              availability_status: "officially_scheduled"
            },
            {
              participant_id: event.away.participant_id,
              source_name: event.away.name,
              official_label: match.awayLabel,
              availability_status: "officially_scheduled"
            }
          ],
          medical_health_claimed: false,
          material_change: false,
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({
        event_id: event.event_id,
        reason: String(error?.message || "official_availability_unavailable")
      });
    }
  }
  return { observations, unavailable };
}

export async function officialTennisScheduleRestObservations({
  sourceKey,
  spec,
  events,
  requestedAt,
  fetchImpl = fetch
}) {
  const observations = [];
  const unavailable = [];
  const pageCache = new Map();
  for (const event of events) {
    const config = TOURNAMENT_DIRECTORY[event.provider_sport_key];
    if (
      !config
      || config.sport !== "wta"
      || event.sport !== "wta"
      || !config.schedule?.scoreUrl
      || spec.category !== "rest_travel"
    ) {
      unavailable.push({ event_id: event.event_id, reason: "unsupported_tournament_provider_key" });
      continue;
    }
    if (
      event.venue?.reviewed !== true
      || normal(event.venue?.city) !== normal(config.schedule.location.city)
      || normal(event.venue?.country_code) !== normal(config.schedule.location.countryCode)
    ) {
      unavailable.push({ event_id: event.event_id, reason: "reviewed_venue_location_required" });
      continue;
    }
    try {
      const year = new Date(event.commence_time).getUTCFullYear();
      const drawUrl = tournamentUrl(config.drawUrl, year);
      const drawPage = await cachedOfficialPage(pageCache, config, drawUrl, year, fetchImpl);
      const currentMatch = exactWtaStatus(drawPage.html, event);
      if (currentMatch.status !== "scheduled") throw new Error("official_active_match_required");
      if (!currentMatch.matchCode) throw new Error("official_match_code_not_found");

      const currentDetailUrl = scoreUrl(config.schedule.scoreUrl, year, currentMatch.matchCode);
      const currentDetailPage = await cachedOfficialPage(
        pageCache,
        config,
        currentDetailUrl,
        year,
        fetchImpl
      );
      if (
        !identityPresent(currentDetailPage.text, event.home.name)
        || !identityPresent(currentDetailPage.text, event.away.name)
      ) {
        throw new Error("exact_official_pair_not_found");
      }
      const currentInfo = matchInfo(currentDetailPage.text);
      // Court/stadium labels on the official score page need not equal the reviewed city.
      // Location integrity remains enforced by reviewed event venue + tournament URL + pair + date.
      const currentDate = officialDate(currentInfo.startTime);
      if (currentDate !== localDate(event.commence_time, config.schedule.location.timeZone)) {
        throw new Error("official_current_date_mismatch");
      }

      const currentTournament = {
        ...config,
        scoreUrl: config.schedule.scoreUrl,
        location: config.schedule.location
      };
      const previous = [];
      for (const participant of [event.home, event.away]) {
        previous.push(await latestPreviousMatch({
          participant,
          currentDate,
          currentYear: year,
          currentMatchCode: currentMatch.matchCode,
          currentTournament,
          previousTournaments: config.schedule.previousTournaments,
          pageCache,
          fetchImpl
        }));
      }

      const participantEvidence = [event.home, event.away].map((participant, index) => {
        const prior = previous[index];
        const daysSincePreviousMatch = calendarDays(prior.date, currentDate);
        const fullRestDays = Math.max(daysSincePreviousMatch - 1, 0);
        const travelDistanceKm = distanceKm(prior.location, config.schedule.location);
        const priorOffset = timeZoneOffsetMinutes(currentDate, prior.location.timeZone);
        const currentOffset = timeZoneOffsetMinutes(currentDate, config.schedule.location.timeZone);
        return {
          participant_id: participant.participant_id,
          source_name: participant.name,
          previous_tournament: prior.tournament,
          previous_match_code: prior.matchCode,
          previous_match_date: prior.date,
          previous_match_url: prior.detailUrl,
          previous_location: prior.location.label,
          current_match_date: currentDate,
          current_location: config.schedule.location.label,
          days_since_previous_match: daysSincePreviousMatch,
          full_rest_days: fullRestDays,
          travel_distance_km: travelDistanceKm,
          time_zone_change_hours: (currentOffset - priorOffset) / 60
        };
      });

      observations.push({
        event_id: event.event_id,
        sport: event.sport,
        source_key: sourceKey,
        category: spec.category,
        verification_status: "verified",
        collected_at: requestedAt,
        payload: {
          event_id: event.event_id,
          provider_sport_key: event.provider_sport_key,
          source_event_id: event.source_event_id,
          tournament: config.tournament,
          official_url: currentDetailUrl,
          schedule_basis: "exact_official_current_and_previous_completed_matches",
          rest_basis: "full_calendar_days_between_official_matches",
          travel_basis: "great_circle_distance_between_reviewed_official_match_locations",
          participants: participantEvidence,
          rest_days_diff: participantEvidence[0].full_rest_days - participantEvidence[1].full_rest_days,
          travel_distance_km_diff:
            participantEvidence[0].travel_distance_km - participantEvidence[1].travel_distance_km,
          time_zone_change_hours_diff:
            participantEvidence[0].time_zone_change_hours
            - participantEvidence[1].time_zone_change_hours,
          material_change: false,
          test_only: true
        }
      });
    } catch (error) {
      unavailable.push({
        event_id: event.event_id,
        reason: String(error?.message || "official_schedule_rest_unavailable")
      });
    }
  }
  return { observations, unavailable };
}
