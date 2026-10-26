# Sports Analytics Pipeline

A Python pipeline for sports data analytics and research automation. It collects
odds, schedule, roster, injury and weather data from public and commercial sports
data APIs, stores it in SQLite, scores upcoming matchups with test-only models,
tracks the results as paper (simulated) decisions, and runs on a schedule with
GitHub Actions. Optional Telegram notifications deliver research summaries and
test results.

The project runs in research / test-only mode. It does not place real-money
transactions, and configuration rejects any mode other than `research` or
`test_only`.

## What it does

- **Data collection** (`src/sports_analytics_pipeline/collectors/`): collectors for
  The Odds API, TheRundown, Odds-API.io history, nflverse (schedules, rosters,
  injuries, play-by-play), the National Weather Service, official NFL/MLB schedule
  sources and additional multi-sport providers. A shared HTTP transport applies
  timeouts and retries, and odds requests are bounded by a credit/quota budget.
- **Storage**: a SQLite schema (`data/schema.sql`) with seeded reference data for
  teams and stadiums.
- **Matchup scoring**: modeling and evaluation modules that compute implied and
  break-even probabilities, expected value, market consensus and pick/pass
  decisions with explicit gates (stale quotes, missing data, correlation checks).
- **Paper tracking and validation**: immutable paper decisions, settlement,
  drawdown, calibration and closing-line-value reporting.
- **Scheduled pipelines**: GitHub Actions workflows in `.github/workflows/` run
  collectors, the paper database, historical backfills, venue sync and research
  reports on cron schedules, plus a test workflow on push and pull request.
- **Notifications**: `scripts/send_telegram_notification.py` and related builders
  send formatted summaries to a Telegram chat through the Bot API.
- **Research adapter**: `research_adapter/` is a Cloudflare Worker that returns
  normalized, event-specific JSON (for example Open-Meteo forecasts) behind a
  bearer token.

## Tech stack

- Python 3.11+, standard library only at runtime (`sqlite3`, `urllib`, `json`)
- SQLite
- pytest test suite (`tests/`, unittest-style and plain test functions)
- GitHub Actions for scheduling and CI
- JavaScript Cloudflare Worker (`research_adapter/`, tested with `node --test`)
- Telegram Bot API for notifications

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install .

cp .env.example .env          # then fill in the keys you have
python scripts/init_db.py      # create the SQLite database
python scripts/run_collectors.py --help
python scripts/run_collectors.py nflverse
python scripts/run_model.py --help
python scripts/run_evaluator.py --help
python scripts/run_report.py
```

Tests:

```bash
pip install pytest
PYTHONPATH=src python -m pytest -q
node --test research_adapter/test/*.test.js
```

## Configuration and secrets

All credentials come from environment variables (or a local `.env` file, which is
git-ignored). `.env.example` lists every setting with secrets left empty. In
GitHub Actions the workflows read them from repository secrets, for example
`${{ secrets.THE_ODDS_API_KEY }}`, `${{ secrets.TELEGRAM_BOT_TOKEN }}` and
`${{ secrets.TELEGRAM_CHAT_ID }}`. No keys, tokens or chat IDs are stored in the
repository.

## CI workflows

The GitHub Actions workflow definitions are kept in `ci/github-workflows/` so they do not run in this showcase copy. To enable them in a fork, move them back to `.github/workflows/` and add the required repository secrets.
