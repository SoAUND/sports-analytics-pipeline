# Multi-Sport Research Adapter

This Cloudflare Worker is the protected adapter service used by the multi-sport
paper workflow. It accepts exact event context from the bot and returns normalized,
event-specific JSON. It never takes a real-money action and every response remains test-only.

## What works now

- All 37 approved research source keys have stable adapter routes.
- Every feed route requires the same bearer token used by GitHub Actions.
- Requests are limited to 100 test-only events and 256 KiB.
- Source keys, sports, and categories are allowlisted in code.
- EPL, ATP, and WTA Open-Meteo routes collect live forecasts when the event has a
  reviewed venue city and country. The exact ATP/WTA Cincinnati provider keys map
  to the reviewed Lindner Family Tennis Center venue in Mason, Ohio, and the exact
  WTA Monterrey provider key maps to Club Sonoma in Monterrey, Mexico.
- The ATP and WTA draw routes verify singles participants only when both player
  identities appear together in one reviewed official draw match block. Cincinnati
  and WTA Monterrey are currently reviewed. Separate blocks, missing names, ambiguous
  matches, and unsupported tournaments fail closed.
- The WTA official-status route verifies a scheduled or final match only when both
  exact player identities share one reviewed official Cincinnati or Monterrey match
  block. It accepts only the reviewed `U` and `F` match codes. Unknown or conflicting
  status signals fail closed. ATP official status remains unavailable until its
  source-specific connection can be verified from the adapter runtime.
- The WTA injury-availability route verifies only that both exact players remain
  together in one officially scheduled match block. It does not claim either player
  is medically healthy. Final matches, explicit withdrawal/walkover/retirement/
  cancellation signals, unknown status codes, and unsupported tournaments fail closed.
- The WTA schedule-and-rest route supports reviewed Monterrey singles matches. It
  verifies the exact current match and each player's latest exact completed match on
  reviewed official WTA draw and score pages. It then reports full calendar rest days,
  great-circle travel distance between reviewed match cities, and time-zone change.
  A missing player history, mismatched date or location, unknown match code, or
  unreviewed venue returns no observation. Cincinnati and Hamburg are the currently
  reviewed prior tournament sources.
- The Tennis Abstract route supports reviewed WTA Cincinnati and Monterrey hard-court
  matches. It verifies each exact player profile and uses the source's current ranking,
  hard-court Elo, last-52-week hard-court serve and return rates, and five completed
  pre-event matches. Same-day, future, incomplete, missing, or ambiguous results are
  excluded or fail closed. The recent-form score is the mean actual result minus the
  rank-based expected result across those five matches.
- Every other route deliberately returns `unavailable` with zero observations until
  that source receives a reviewed source-specific connector. A reachable webpage is
  never promoted to verified evidence.

## Deploy from the Cloudflare dashboard

1. Open Cloudflare and select **Workers & Pages**.
2. Select **Create application**, then **Import a repository**.
3. Connect GitHub if prompted and select the `OWNER/sports-analytics-pipeline` repository.
4. Set the project root directory to `research_adapter`.
5. Keep the deploy command as `npx wrangler deploy`.
6. Deploy the Worker.
7. Open the Worker, then select **Settings > Variables and Secrets**.
8. Add an encrypted secret named `MULTISPORT_RESEARCH_FEED_TOKEN`.
9. Generate a long random value, save it in a password manager, and use the exact
   same value for the GitHub Actions secret with that name. Never commit it.
10. Open `https://YOUR-WORKER.workers.dev/health`. It should return `status: ok`.
11. Open `https://YOUR-WORKER.workers.dev/configuration`. Copy only the JSON object
    inside `MULTISPORT_RESEARCH_FEEDS_JSON`.
12. In GitHub, open **Settings > Secrets and variables > Actions > Variables** and
    create `MULTISPORT_RESEARCH_FEEDS_JSON` with that copied JSON object.
13. In the **Secrets** tab, create `MULTISPORT_RESEARCH_FEED_TOKEN` with the same
    protected value used in Cloudflare.
14. Run **Multi-Sport Paper Database** manually with provider checks disabled for the
    first adapter-only verification.

## Local verification

```bash
cd research_adapter
npm test
```

For local development, create an uncommitted `.dev.vars` file:

```text
MULTISPORT_RESEARCH_FEED_TOKEN=replace-with-a-long-random-local-token
```

Then run `npm install` followed by `npm run dev`.

## Safe endpoint behavior

| Endpoint | Authentication | Purpose |
|---|---:|---|
| `GET /health` | No | Safe service health only |
| `GET /manifest` | No | Approved source keys and implementation status |
| `GET /configuration` | No | Generates adapter URLs for GitHub configuration |
| `POST /feeds/:source_key` | Bearer token | Collects normalized event evidence |

The public endpoints expose no token, observations, or private configuration.
