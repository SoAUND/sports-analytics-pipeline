import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/index.js";
import { SOURCE_DIRECTORY } from "../src/sources.js";

const ENV = { MULTISPORT_RESEARCH_FEED_TOKEN: "test-secret" };
const REQUESTED_AT = "2026-08-22T18:00:00.000Z";

function event(overrides = {}) {
  return {
    event_id: "epl:soccer_epl:event-1",
    sport: "epl",
    provider_sport_key: "soccer_epl",
    source_event_id: "event-1",
    commence_time: "2026-08-22T20:00:00.000Z",
    home: { participant_id: "epl:home", name: "Home" },
    away: { participant_id: "epl:away", name: "Away" },
    venue: {
      venue_id: "emirates_stadium",
      venue_name: "Emirates Stadium",
      city: "London",
      country_code: "GB",
      reviewed: true
    },
    test_only: true,
    ...overrides
  };
}

function tennisEvent(sport, overrides = {}) {
  const atp = sport === "atp";
  return event({
    event_id: `${sport}:tennis_${sport}_cincinnati_open:event-1`,
    sport,
    provider_sport_key: `tennis_${sport}_cincinnati_open`,
    source_event_id: "event-1",
    commence_time: "2026-08-22T20:00:00.000Z",
    home: atp
      ? { participant_id: "atp:nakashima", name: "Brandon Nakashima" }
      : { participant_id: "wta:bejlek", name: "Sara Bejlek" },
    away: atp
      ? { participant_id: "atp:tiafoe", name: "Frances Tiafoe" }
      : { participant_id: "wta:gauff", name: "Coco Gauff" },
    venue: {
      venue_id: "lindner_family_tennis_center",
      venue_name: "Lindner Family Tennis Center",
      city: "Mason",
      country_code: "US",
      reviewed: true
    },
    ...overrides
  });
}

function monterreyEvent(overrides = {}) {
  return tennisEvent("wta", {
    event_id: "wta:tennis_wta_monterrey_open:event-1",
    provider_sport_key: "tennis_wta_monterrey_open",
    commence_time: "2026-08-24T20:00:00.000Z",
    home: { participant_id: "wta:tauson", name: "Clara Tauson" },
    away: { participant_id: "wta:zarazua", name: "Renata Zarazua" },
    venue: {
      venue_id: "club_sonoma",
      venue_name: "Club Sonoma",
      city: "Monterrey",
      country_code: "MX",
      reviewed: true
    },
    ...overrides
  });
}

function adapterRequest(sourceKey, events = [event()]) {
  return new Request(`https://adapter.example/feeds/${sourceKey}`, {
    method: "POST",
    headers: {
      Authorization: "Bearer test-secret",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      contract_version: 1,
      source_key: sourceKey,
      categories: [SOURCE_DIRECTORY[sourceKey].category],
      requested_at: REQUESTED_AT,
      events,
      test_only: true
    })
  });
}

function cells(values) {
  return `<tr>${values.map((value) => `<td>${value}</td>`).join("")}</tr>`;
}

function tennisAbstractPage(name, rank, slug = name.replace(/[^a-z0-9]/giu, "")) {
  return `<html><head><title>Tennis Abstract: ${name}</title>
    <script src="https://www.tennisabstract.com/jsfrags/${slug}.js"></script>
    <script>var fullname = '${name}'; var currentrank = ${rank};</script>
    </head><body></body></html>`;
}

function tennisAbstractFragment({ name, rank, surfaceElo, serve, returnPoints, results = [1, 1, 0, 1, 0] }) {
  const surname = name.split(" ").at(-1);
  const recentRows = [
    cells(["24-Aug-2026", "Monterrey", "Hard", "R32", rank, 999, `<b>${surname}</b> vs Future Player`, "", ""])
  ];
  results.forEach((won, index) => {
    const opponentRank = [10, 25, 40, 8, 80][index];
    const outcome = won
      ? `<b>${surname}</b> d. <a>Opponent ${index}</a>`
      : `<a>Opponent ${index}</a> d. <b>${surname}</b>`;
    recentRows.push(cells([
      `${String(20 - index).padStart(2, "0")}-Aug-2026`,
      `Prior ${index}`,
      "Hard",
      "R32",
      rank + index,
      opponentRank,
      outcome,
      "6-4 6-4",
      "1.10"
    ]));
  });
  const ratingRow = cells([
    "<b><i>Current (2026-08-10)</i></b>", rank, 1000, 25, 1800,
    20, surfaceElo, 30, 1700, 40, 1600
  ]);
  const split = Array(23).fill("");
  split[0] = '<a>Hard</a>';
  split[1] = 12;
  split[11] = 10;
  split[19] = `${serve}%`;
  split[20] = `${returnPoints}%`;
  return `var player_frag = \`
    <table id="recent-results"><tbody>${recentRows.join("")}</tbody></table>
    <table id="year-end-rankings"><tbody>${ratingRow}</tbody></table>
    <table id="last52-splits"><tbody>${cells(split)}</tbody></table>
  \`;`;
}

test("health and generated configuration expose no secret", async () => {
  const health = await handleRequest(new Request("https://adapter.example/health"), ENV);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), {
    status: "ok",
    service: "our-multisport-research-adapter",
    test_only: true
  });
  const configuration = await handleRequest(new Request("https://adapter.example/configuration"), ENV);
  const payload = await configuration.json();
  assert.equal(Object.keys(payload.MULTISPORT_RESEARCH_FEEDS_JSON).length, 37);
  assert.equal(
    payload.MULTISPORT_RESEARCH_FEEDS_JSON.nba_official,
    "https://adapter.example/feeds/nba_official"
  );
  assert.equal(JSON.stringify(payload).includes("test-secret"), false);
});

test("feed routes require the configured bearer token", async () => {
  const request = adapterRequest("nba_official", [event({ sport: "nba" })]);
  const unauthorized = new Request(request.url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: await request.text()
  });
  const response = await handleRequest(unauthorized, ENV);
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { error: "unauthorized" });
});

test("unverified source connectors fail closed with no observations", async () => {
  const response = await handleRequest(
    adapterRequest("nba_official", [event({ sport: "nba" })]),
    ENV
  );
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "source_specific_connector_not_yet_verified");
});

test("Tennis Abstract builds the complete reviewed WTA statistics feature set", async () => {
  const profiles = {
    ClaraTauson: {
      page: tennisAbstractPage("Clara Tauson", 12),
      fragment: tennisAbstractFragment({
        name: "Clara Tauson", rank: 12, surfaceElo: 1900, serve: 64.5, returnPoints: 43,
        results: [1, 1, 0, 1, 0]
      })
    },
    RenataZarazua: {
      page: tennisAbstractPage("Renata Zarazua", 20),
      fragment: tennisAbstractFragment({
        name: "Renata Zarazua", rank: 20, surfaceElo: 1800, serve: 61, returnPoints: 45,
        results: [0, 1, 1, 0, 1]
      })
    }
  };
  let calls = 0;
  const response = await handleRequest(
    adapterRequest("tennis_abstract", [monterreyEvent()]),
    ENV,
    async (input) => {
      calls += 1;
      const url = new URL(input);
      const slug = url.pathname.startsWith("/jsfrags/")
        ? url.pathname.split("/").at(-1).replace(".js", "")
        : url.searchParams.get("p");
      const profile = profiles[slug];
      assert.ok(profile, `unexpected Tennis Abstract player URL: ${url}`);
      const fragment = url.pathname.startsWith("/jsfrags/");
      return new Response(fragment ? profile.fragment : profile.page, {
        status: 200,
        headers: { "Content-Type": fragment ? "text/javascript" : "text/html" }
      });
    }
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  const evidence = payload.observations[0].payload;
  assert.equal(evidence.ranking_diff, 8);
  assert.equal(evidence.surface_elo_diff, 100);
  assert.equal(evidence.serve_points_won_diff, 3.5);
  assert.equal(evidence.return_points_won_diff, -2);
  assert.equal(Number.isFinite(evidence.recent_form_diff), true);
  assert.equal(evidence.participants.length, 2);
  assert.equal(evidence.participants[0].recent_form_matches.length, 5);
  assert.equal(
    evidence.participants.flatMap((row) => row.recent_form_matches).every((row) => row.date < "2026-08-24"),
    true
  );
  assert.equal(evidence.source_data_date, "2026-08-10");
  assert.equal(evidence.test_only, true);
  assert.equal(calls, 4);
});

test("Tennis Abstract refuses a player identity mismatch", async () => {
  const response = await handleRequest(
    adapterRequest("tennis_abstract", [monterreyEvent()]),
    ENV,
    async () => new Response(tennisAbstractPage("Different Player", 12, "ClaraTauson"), {
      status: 200,
      headers: { "Content-Type": "text/html" }
    })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "tennis_abstract_player_identity_mismatch");
});

test("Tennis Abstract refuses an incomplete five-match pre-event form sample", async () => {
  const page = tennisAbstractPage("Clara Tauson", 12);
  const fragment = tennisAbstractFragment({
    name: "Clara Tauson", rank: 12, surfaceElo: 1900, serve: 64.5, returnPoints: 43,
    results: [1, 0, 1, 0]
  });
  const response = await handleRequest(
    adapterRequest("tennis_abstract", [monterreyEvent()]),
    ENV,
    async (input) => new Response(String(input).includes("/jsfrags/") ? fragment : page, {
      status: 200,
      headers: { "Content-Type": String(input).includes("/jsfrags/") ? "text/javascript" : "text/html" }
    })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "tennis_abstract_recent_form_sample_incomplete");
});

test("Tennis Abstract keeps unreviewed ATP tournaments unavailable without fetching", async () => {
  let calls = 0;
  const response = await handleRequest(
    adapterRequest("tennis_abstract", [tennisEvent("atp")]),
    ENV,
    async () => { calls += 1; throw new Error("should not fetch"); }
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.equal(payload.unavailable[0].reason, "unsupported_tournament_provider_key");
  assert.equal(calls, 0);
});

test("Open-Meteo creates only event-specific weather evidence", async () => {
  const fetchImpl = async (input) => {
    const url = new URL(input);
    if (url.hostname === "geocoding-api.open-meteo.com") {
      return new Response(JSON.stringify({
        results: [{ name: "London", country_code: "GB", latitude: 51.5085, longitude: -0.1257 }]
      }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    assert.equal(url.hostname, "api.open-meteo.com");
    return new Response(JSON.stringify({
      hourly_units: {
        temperature_2m: "°C",
        precipitation_probability: "%",
        precipitation: "mm",
        wind_speed_10m: "km/h",
        wind_gusts_10m: "km/h"
      },
      hourly: {
        time: ["2026-08-22T19:00", "2026-08-22T20:00"],
        temperature_2m: [19.1, 18.4],
        precipitation_probability: [20, 30],
        precipitation: [0, 0.2],
        weather_code: [1, 2],
        wind_speed_10m: [11, 14],
        wind_gusts_10m: [20, 27]
      }
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const response = await handleRequest(adapterRequest("open_meteo_epl"), ENV, fetchImpl);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  assert.equal(payload.observations[0].event_id, "epl:soccer_epl:event-1");
  assert.equal(payload.observations[0].category, "weather_environment");
  assert.equal(payload.observations[0].verification_status, "verified");
  assert.equal(payload.observations[0].payload.forecast_time, "2026-08-22T20:00");
  assert.equal(payload.observations[0].payload.wind_gusts_10m, 27);
  assert.equal(payload.observations[0].payload.test_only, true);
});

test("Open-Meteo refuses an unreviewed or missing venue", async () => {
  let calls = 0;
  const response = await handleRequest(
    adapterRequest("open_meteo_epl", [event({ venue: null })]),
    ENV,
    async () => { calls += 1; throw new Error("should not fetch"); }
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "reviewed_venue_location_required");
  assert.equal(calls, 0);
});

test("ATP draw verifies participants only inside one official match segment", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>13 - 23 August, 2026</p>
    <section><h2>Semifinals</h2><a>B. Nakashima (27)</a><a>F. Tiafoe (17)</a><a>H2H</a></section>
    <section><a>A. Fils (21)</a><a>F. Cobolli (7)</a><a>H2H</a></section>
    </body></html>`;
  let calls = 0;
  const response = await handleRequest(adapterRequest("atp_draws", [tennisEvent("atp")]), ENV, async (input) => {
    calls += 1;
    assert.equal(String(input), "https://www.atptour.com/en/scores/current/cincinnati/422/draws");
    return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
  });
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  assert.equal(payload.observations[0].category, "lineup_or_participants");
  assert.equal(payload.observations[0].verification_status, "verified");
  assert.equal(payload.observations[0].payload.participants[0].official_label, "B. Nakashima (27)");
  assert.equal(payload.observations[0].payload.participants[1].official_label, "F. Tiafoe (17)");
  assert.equal(calls, 1);
});

test("WTA draw verifies one exact official pairing", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>S. Bejlek</a><a>C. Gauff (4)</a>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>J. Pegula</a><a>A. Anisimova</a>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta_draws", [tennisEvent("wta")]), ENV, async (input) => {
    assert.equal(String(input), "https://www.wtatennis.com/tournaments/cincinnati-open/draws");
    return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
  });
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  assert.deepEqual(
    payload.observations[0].payload.participants.map((row) => row.official_label),
    ["S. Bejlek", "C. Gauff (4)"]
  );
});

test("WTA Monterrey draw uses the reviewed year-specific official page", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>C. Tauson</a><a>R. Zarazua</a>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>M. Sherif</a><a>O. Oliynykova</a>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta_draws", [monterreyEvent()]),
    ENV,
    async (input) => {
      assert.equal(
        String(input),
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/draws"
      );
      return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
    }
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.deepEqual(
    payload.observations[0].payload.participants.map((row) => row.official_label),
    ["C. Tauson", "R. Zarazua"]
  );
  assert.equal(payload.observations[0].payload.tournament, "Abierto GNP Seguros");
});

test("official tennis draws reject names found in separate match segments", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>S. Bejlek</a><a>M. Keys</a>
    <div>Upcoming</div><div>Suspended</div><div>Finished</div>
    <a>C. Gauff</a><a>A. Sabalenka</a>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta_draws", [tennisEvent("wta")]), ENV, async () =>
    new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "exact_official_pair_not_found");
});

test("official tennis draws reject unreviewed tournament provider keys without fetching", async () => {
  let calls = 0;
  const response = await handleRequest(
    adapterRequest("atp_draws", [tennisEvent("atp", { provider_sport_key: "tennis_atp_unreviewed" })]),
    ENV,
    async () => { calls += 1; throw new Error("should not fetch"); }
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.equal(payload.unavailable[0].reason, "unsupported_tournament_provider_key");
  assert.equal(calls, 0);
});

test("WTA official status reads the structured code from the exact match block", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div class="tennis-match tennis-match--slim js-match-1017-2026-LS002" data-status="U">
      <div>Upcoming</div><div>Suspended</div><div>Finished</div>
      <table><tr><td>S. Bejlek</td></tr><tr><td>C. Gauff (4)</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta", [tennisEvent("wta")]), ENV, async (input) => {
    assert.equal(String(input), "https://www.wtatennis.com/tournaments/cincinnati-open/draws");
    return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
  });
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations[0].payload.game_status, "scheduled");
  assert.equal(payload.observations[0].payload.official_status_code, "U");
  assert.equal(payload.observations[0].payload.official_status_label, null);
});

test("WTA Monterrey official status verifies the exact scheduled match block", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="U">
      <table><tr><td>C. Tauson</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta", [monterreyEvent()]),
    ENV,
    async (input) => {
      assert.equal(
        String(input),
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/draws"
      );
      return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
    }
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations[0].payload.game_status, "scheduled");
  assert.equal(payload.observations[0].payload.official_status_code, "U");
  assert.equal(payload.observations[0].payload.tournament, "Abierto GNP Seguros");
});

test("WTA official status verifies a reviewed final code", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div class="tennis-match js-match-1017-2026-LS002" data-status="F">
      <table><tr><td>S. Bejlek</td></tr><tr><td>C. Gauff (4)</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta", [tennisEvent("wta")]), ENV, async () =>
    new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations[0].payload.game_status, "final");
  assert.equal(payload.observations[0].payload.official_status_code, "F");
  assert.equal(payload.observations[0].payload.material_change, true);
});

test("WTA official status rejects an unreviewed structured code", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div class="tennis-match js-match-1017-2026-LS002" data-status="X">
      <table><tr><td>S. Bejlek</td></tr><tr><td>C. Gauff (4)</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta", [tennisEvent("wta")]), ENV, async () =>
    new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "unreviewed_official_status_code");
});

test("WTA official status rejects players found in separate match blocks", async () => {
  const html = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div class="tennis-match js-match-1" data-status="U">
      <table><tr><td>S. Bejlek</td></tr><tr><td>M. Keys</td></tr></table>
    </div>
    <div class="tennis-match js-match-2" data-status="U">
      <table><tr><td>C. Gauff</td></tr><tr><td>A. Sabalenka</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(adapterRequest("wta", [tennisEvent("wta")]), ENV, async () =>
    new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.equal(payload.unavailable[0].reason, "exact_official_pair_not_found");
});

test("WTA official availability verifies an exact actively scheduled pairing", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="U">
      <table><tr><td>C. Tauson</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta_status", [monterreyEvent()]),
    ENV,
    async (input) => {
      assert.equal(
        String(input),
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/draws"
      );
      return new Response(html, { status: 200, headers: { "Content-Type": "text/html" } });
    }
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations[0].category, "injury_availability");
  assert.equal(payload.observations[0].verification_status, "verified");
  assert.equal(
    payload.observations[0].payload.availability_basis,
    "official_active_scheduled_match"
  );
  assert.deepEqual(
    payload.observations[0].payload.participants.map((row) => row.availability_status),
    ["officially_scheduled", "officially_scheduled"]
  );
  assert.equal(payload.observations[0].payload.medical_health_claimed, false);
});

test("WTA official availability rejects a final match", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="F">
      <table><tr><td>C. Tauson</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta_status", [monterreyEvent()]),
    ENV,
    async () => new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "official_active_match_required");
});

test("WTA official availability rejects an explicit unavailability signal", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="U">
      <table><tr><td>C. Tauson</td><td>W/O</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta_status", [monterreyEvent()]),
    ENV,
    async () => new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "official_unavailability_signal:w_o");
});

test("WTA official availability rejects an unreviewed structured code", async () => {
  const html = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="X">
      <table><tr><td>C. Tauson</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const response = await handleRequest(
    adapterRequest("wta_status", [monterreyEvent()]),
    ENV,
    async () => new Response(html, { status: 200, headers: { "Content-Type": "text/html" } })
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "unreviewed_official_status_code");
});

function monterreySchedulePages({ currentDate = "Mon 24 Aug 2026", includeZarazua = true } = {}) {
  const currentDraw = `
    <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
    <div class="tennis-match js-match-1039-2026-LS001" data-status="U">
      <table><tr><td>C. Tauson</td></tr><tr><td>R. Zarazua</td></tr></table>
    </div>
    </body></html>`;
  const currentDetail = `
    <html><body><h1>Abierto GNP Seguros</h1><p>2026</p>
    <p>C. Tauson</p><p>R. Zarazua</p><h2>Match Info</h2>
    <div>Start Time</div><div>${currentDate}</div>
    <div>Location</div><div>MONTERREY, MEX</div>
    </body></html>`;
  const cincinnatiDraw = `
    <html><body><h1>Cincinnati Open</h1><p>Aug 13 - Aug 23, 2026</p>
    <div class="tennis-match js-match-1017-2026-LS024" data-status="F">
      <table><tr><td>L. Noskova</td></tr><tr><td>C. Tauson</td></tr></table>
    </div>
    ${includeZarazua ? `
    <div class="tennis-match js-match-1017-2026-RS034" data-status="F">
      <table><tr><td>R. Zarazua</td></tr><tr><td>Y. Yuan</td></tr></table>
    </div>` : ""}
    </body></html>`;
  const tausonDetail = `
    <html><body><h1>Cincinnati Open</h1><p>2026</p><p>C. Tauson</p>
    <h2>Match Info</h2><div>Start Time</div><div>Mon 17 Aug 2026</div>
    <div>Location</div><div>CINCINNATI, USA</div></body></html>`;
  const zarazuaDetail = `
    <html><body><h1>Cincinnati Open</h1><p>2026</p><p>R. Zarazua</p>
    <h2>Match Info</h2><div>Start Time</div><div>Tue 11 Aug 2026</div>
    <div>Location</div><div>CINCINNATI, USA</div></body></html>`;
  const hamburgDraw = `
    <html><body><h1>MSC Hamburg Ladies Open</h1><p>2026</p></body></html>`;
  return {
    currentDraw,
    currentDetail,
    cincinnatiDraw,
    hamburgDraw,
    tausonDetail,
    zarazuaDetail
  };
}

function scheduleFetch(pages, calls = []) {
  return async (input) => {
    const url = String(input);
    calls.push(url);
    const page = new Map([
      ["https://www.wtatennis.com/tournaments/1039/monterrey/2026/draws", pages.currentDraw],
      ["https://www.wtatennis.com/tournaments/1039/monterrey/2026/scores/LS001", pages.currentDetail],
      ["https://www.wtatennis.com/tournaments/1017/cincinnati/2026/draws", pages.cincinnatiDraw],
      ["https://www.wtatennis.com/tournaments/1017/cincinnati/2026/scores/LS024", pages.tausonDetail],
      ["https://www.wtatennis.com/tournaments/1017/cincinnati/2026/scores/RS034", pages.zarazuaDetail],
      ["https://www.wtatennis.com/tournaments/2042/hamburg/2026/draws", pages.hamburgDraw],
      ...(pages.extraPages || [])
    ]).get(url);
    assert.ok(page, `unexpected official WTA URL: ${url}`);
    return new Response(page, { status: 200, headers: { "Content-Type": "text/html" } });
  };
}

test("WTA schedule and rest derives exact rest and travel evidence", async () => {
  const calls = [];
  const response = await handleRequest(
    adapterRequest("wta_schedule_rest", [monterreyEvent()]),
    ENV,
    scheduleFetch(monterreySchedulePages(), calls)
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  const evidence = payload.observations[0].payload;
  assert.equal(evidence.rest_days_diff, -6);
  assert.equal(evidence.travel_distance_km_diff, 0);
  assert.equal(evidence.time_zone_change_hours_diff, 0);
  assert.deepEqual(
    evidence.participants.map((row) => row.full_rest_days),
    [6, 12]
  );
  assert.deepEqual(
    evidence.participants.map((row) => row.time_zone_change_hours),
    [-2, -2]
  );
  assert.ok(evidence.participants[0].travel_distance_km > 1900);
  assert.equal(evidence.test_only, true);
  assert.equal(calls.filter((url) => url.endsWith("/draws")).length, 3);
});

test("WTA schedule and rest uses reviewed Hamburg history when it is latest", async () => {
  const pages = {
    currentDraw: `
      <html><body><h1>Abierto GNP Seguros</h1><p>Aug 23 - Aug 29, 2026</p>
      <div class="tennis-match js-match-1039-2026-LS018" data-status="U">
        <table><tr><td>M. Sherif</td></tr><tr><td>O. Oliynykova</td></tr></table>
      </div></body></html>`,
    currentDetail: "unused",
    cincinnatiDraw: `
      <html><body><h1>Cincinnati Open</h1><p>2026</p>
      <div class="tennis-match js-match-1017-2026-LS047" data-status="F">
        <table><tr><td>O. Oliynykova</td></tr><tr><td>M. Andreeva</td></tr></table>
      </div></body></html>`,
    hamburgDraw: `
      <html><body><h1>MSC Hamburg Ladies Open</h1><p>2026</p>
      <div class="tennis-match js-match-2042-2026-LS003" data-status="F">
        <table><tr><td>M. Sherif</td></tr><tr><td>T. Korpatsch</td></tr></table>
      </div></body></html>`,
    tausonDetail: "unused",
    zarazuaDetail: "unused",
    extraPages: [
      [
        "https://www.wtatennis.com/tournaments/1039/monterrey/2026/scores/LS018",
        `<html><body><h1>Abierto GNP Seguros</h1><p>2026</p>
        <p>M. Sherif</p><p>O. Oliynykova</p><h2>Match Info</h2>
        <div>Start Time</div><div>Sun 23 Aug 2026</div>
        <div>Location</div><div>MONTERREY, MEX</div></body></html>`
      ],
      [
        "https://www.wtatennis.com/tournaments/1017/cincinnati/2026/scores/LS047",
        `<html><body><h1>Cincinnati Open</h1><p>2026</p><p>O. Oliynykova</p>
        <h2>Match Info</h2><div>Start Time</div><div>Sun 16 Aug 2026</div>
        <div>Location</div><div>CINCINNATI, USA</div></body></html>`
      ],
      [
        "https://www.wtatennis.com/tournaments/2042/hamburg/2026/scores/LS003",
        `<html><body><h1>MSC Hamburg Ladies Open</h1><p>2026</p><p>M. Sherif</p>
        <h2>Match Info</h2><div>Start Time</div><div>Sat 25 Jul 2026</div>
        <div>Location</div><div>HAMBURG, GER</div></body></html>`
      ]
    ]
  };
  const response = await handleRequest(
    adapterRequest("wta_schedule_rest", [monterreyEvent({
      commence_time: "2026-08-23T20:00:00.000Z",
      home: { participant_id: "wta:sherif", name: "Mayar Sherif" },
      away: { participant_id: "wta:oliynykova", name: "Oleksandra Oliynykova" }
    })]),
    ENV,
    scheduleFetch(pages)
  );
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.deepEqual(
    payload.observations[0].payload.participants.map((row) => row.previous_tournament),
    ["MSC Hamburg Ladies Open", "Cincinnati Open"]
  );
  assert.deepEqual(
    payload.observations[0].payload.participants.map((row) => row.full_rest_days),
    [28, 6]
  );
});

test("WTA schedule and rest fails closed when either prior match is missing", async () => {
  const response = await handleRequest(
    adapterRequest("wta_schedule_rest", [monterreyEvent()]),
    ENV,
    scheduleFetch(monterreySchedulePages({ includeZarazua: false }))
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(
    payload.unavailable[0].reason,
    "official_previous_match_not_found:Renata Zarazua"
  );
});

test("WTA schedule and rest rejects a current official date mismatch", async () => {
  const response = await handleRequest(
    adapterRequest("wta_schedule_rest", [monterreyEvent()]),
    ENV,
    scheduleFetch(monterreySchedulePages({ currentDate: "Sun 23 Aug 2026" }))
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.deepEqual(payload.observations, []);
  assert.equal(payload.unavailable[0].reason, "official_current_date_mismatch");
});

test("WTA schedule and rest requires the reviewed Monterrey venue", async () => {
  let calls = 0;
  const response = await handleRequest(
    adapterRequest("wta_schedule_rest", [monterreyEvent({
      venue: { venue_id: "other", city: "Monterrey", country_code: "MX", reviewed: false }
    })]),
    ENV,
    async () => { calls += 1; throw new Error("should not fetch"); }
  );
  const payload = await response.json();
  assert.equal(payload.status, "unavailable");
  assert.equal(payload.unavailable[0].reason, "reviewed_venue_location_required");
  assert.equal(calls, 0);
});

test("source and sport mismatches are rejected", async () => {
  const response = await handleRequest(
    adapterRequest("nba_official", [event({ sport: "epl" })]),
    ENV
  );
  assert.equal(response.status, 400);
  assert.equal((await response.json()).reason, "invalid_event_identity");
});

test("official Premier League statistics derive only pre-kickoff baseline evidence", async () => {
  const current = {
    id: 99,
    status: "U",
    kickoff: { millis: new Date("2026-08-22T20:00:00.000Z").getTime() },
    teams: [{ team: { id: 1, name: "Home" } }, { team: { id: 2, name: "Away" } }]
  };
  const prior = [];
  for (let index = 1; index <= 5; index += 1) {
    prior.push({
      id: index,
      status: "C",
      kickoff: { millis: current.kickoff.millis - index * 7 * 86400000 },
      teams: [
        { team: { id: 1, name: "Home" }, score: { score: 2 } },
        { team: { id: 100 + index, name: `Home Opponent ${index}` }, score: { score: 1 } }
      ]
    });
    prior.push({
      id: 20 + index,
      status: "C",
      kickoff: { millis: current.kickoff.millis - index * 6 * 86400000 },
      teams: [
        { team: { id: 200 + index, name: `Away Opponent ${index}` }, score: { score: 1 } },
        { team: { id: 2, name: "Away" }, score: { score: 0 } }
      ]
    });
  }
  const fetchImpl = async (url) => {
    const value = String(url);
    if (value.includes("compseasons")) return new Response(JSON.stringify({ content: [{ id: 841 }, { id: 777 }] }), { status: 200 });
    if (value.includes("/fixtures/99")) return new Response(JSON.stringify({ ...current, teamLists: [] }), { status: 200 });
    return new Response(JSON.stringify({ content: [...prior, current] }), { status: 200 });
  };
  const response = await handleRequest(adapterRequest("premier_league_statistics"), ENV, fetchImpl);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.status, "collected");
  assert.equal(payload.observations.length, 1);
  assert.deepEqual(Object.keys(payload.observations[0].payload.baseline_features).sort(), [
    "fixture_congestion_diff", "home_away_strength_diff", "recent5_points_diff", "rest_days_diff"
  ]);
  assert.equal(payload.observations[0].payload.derivation, "official_pre_kickoff_results_only");
});

test("legacy FBref URL is only a transport alias for the official EPL statistics source", async () => {
  const request = adapterRequest("premier_league_statistics");
  const aliasRequest = new Request("https://adapter.example/feeds/fbref", request);
  const fetchImpl = async (url) => {
    if (String(url).includes("compseasons")) return new Response(JSON.stringify({ content: [{ id: 841 }] }), { status: 200 });
    return new Response(JSON.stringify({ content: [] }), { status: 200 });
  };
  const response = await handleRequest(aliasRequest, ENV, fetchImpl);
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.source_key, "premier_league_statistics");
});

test("official UFC identity connector verifies exact UFC pair and rejects non-UFC pair", async () => {
  const listed = event({event_id:"mma:mma_mixed_martial_arts:ufc",sport:"mma",provider_sport_key:"mma_mixed_martial_arts",source_event_id:"ufc",home:{participant_id:"mma:umar",name:"Umar Nurmagomedov"},away:{participant_id:"mma:song",name:"Song Yadong"},venue:null});
  const html=`<html><body><div><p>Umar Nurmagomedov</p><p>vs</p><p>Song Yadong</p></div></body></html>`;
  const f=async()=>new Response(html,{status:200,headers:{"Content-Type":"text/html"}});
  let response=await handleRequest(adapterRequest("ufc_event",[listed]),ENV,f); let payload=await response.json();
  assert.equal(payload.status,"collected"); assert.equal(payload.observations[0].payload.promotion,"UFC");
  const generic={...listed,event_id:"mma:mma_mixed_martial_arts:other",home:{participant_id:"mma:perry",name:"Mike Perry"},away:{participant_id:"mma:jenkins",name:"Brandon Jenkins"}};
  response=await handleRequest(adapterRequest("ufc_event",[generic]),ENV,f); payload=await response.json();
  assert.equal(payload.status,"unavailable"); assert.equal(payload.unavailable[0].reason,"exact_official_ufc_pair_not_found");
});
