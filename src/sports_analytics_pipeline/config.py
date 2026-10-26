"""Protected runtime configuration for our betting research system."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import os


ALLOWED_MODES = {"test_only", "research"}
REQUIRED_PROVIDER_KEYS = (
    "the_odds_api",
    "oddspapi",
    "odds_api_io",
    "sports_game_odds",
    "api_sports",
    "cfbd",
    "football_data",
)


def _read_env_file(path: Path) -> dict[str, str]:
    """Read a small KEY=VALUE file without overwriting process settings."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid environment setting on line {line_number}")
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _json_string_map(value: str | None, *, setting_name: str) -> dict[str, str]:
    """Parse a small JSON object without ever logging its values."""
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{setting_name} must be a valid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{setting_name} must be a JSON object")
    result: dict[str, str] = {}
    for raw_key, raw_url in payload.items():
        key = str(raw_key).casefold().strip()
        url = str(raw_url).strip()
        if not key or not url:
            raise ValueError(f"{setting_name} cannot contain blank source keys or URLs")
        result[key] = url
    return result


@dataclass(frozen=True)
class Settings:
    database_path: Path
    bot_mode: str
    odds_api_key: str | None
    therundown_api_key: str | None
    oddspapi_api_key: str | None
    odds_api_io_key: str | None
    sports_game_odds_api_key: str | None
    api_sports_key: str | None
    cfbd_api_key: str | None
    football_data_api_key: str | None
    market_consensus_bookmakers: tuple[str, ...]
    accessible_bookmakers: tuple[str, ...]
    odds_stale_after_minutes: int
    nws_user_agent: str
    http_timeout_seconds: int
    http_retry_attempts: int
    odds_api_min_remaining_credits: int
    odds_api_daily_credit_cap: int
    multisport_odds_horizon_minutes: int
    multisport_odds_refresh_minutes: int
    multisport_score_poll_minutes: int
    multisport_mma_result_poll_minutes: int
    multisport_participant_refresh_hours: int
    multisport_history_lookback_days: int
    multisport_history_max_requests: int
    multisport_history_max_leagues_per_sport: int
    multisport_history_max_odds_events: int
    odds_api_io_history_bookmakers: tuple[str, ...]
    multisport_research_feed_urls: dict[str, str]
    multisport_research_feed_token: str | None
    multisport_research_max_requests: int
    multisport_research_horizon_minutes: int

    nhl_cfb_odds_api_key: str | None = field(default=None, repr=False)
    cfbd_2_api_key: str | None = field(default=None, repr=False)

    @classmethod
    def load(cls, env_file: str | Path = ".env") -> "Settings":
        file_values = _read_env_file(Path(env_file))

        def value(name: str, default: str = "") -> str:
            return os.environ.get(name, file_values.get(name, default))

        mode = value("BOT_MODE", "research")
        if mode not in ALLOWED_MODES:
            raise ValueError("BOT_MODE must be research or test_only; neither mode takes real-money actions")

        stale_minutes = int(value("ODDS_STALE_AFTER_MINUTES", "60"))
        if stale_minutes < 1:
            raise ValueError("ODDS_STALE_AFTER_MINUTES must be at least 1")

        timeout_seconds = int(value("HTTP_TIMEOUT_SECONDS", "20"))
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise ValueError("HTTP_TIMEOUT_SECONDS must be between 1 and 120")

        retry_attempts = int(value("HTTP_RETRY_ATTEMPTS", "3"))
        if retry_attempts < 1 or retry_attempts > 5:
            raise ValueError("HTTP_RETRY_ATTEMPTS must be between 1 and 5")

        min_remaining = int(value("ODDS_API_MIN_REMAINING_CREDITS", "50"))
        if min_remaining < 0:
            raise ValueError("ODDS_API_MIN_REMAINING_CREDITS cannot be negative")

        daily_credit_cap = int(value("ODDS_API_DAILY_CREDIT_CAP", "10"))
        if daily_credit_cap < 1 or daily_credit_cap > 100:
            raise ValueError("ODDS_API_DAILY_CREDIT_CAP must be between 1 and 100")

        odds_horizon = int(value("MULTISPORT_ODDS_HORIZON_MINUTES", "90"))
        if odds_horizon < 30 or odds_horizon > 360:
            raise ValueError("MULTISPORT_ODDS_HORIZON_MINUTES must be between 30 and 360")

        odds_refresh = int(value("MULTISPORT_ODDS_REFRESH_MINUTES", "30"))
        if odds_refresh < 15 or odds_refresh > 180:
            raise ValueError("MULTISPORT_ODDS_REFRESH_MINUTES must be between 15 and 180")

        score_poll = int(value("MULTISPORT_SCORE_POLL_MINUTES", "720"))
        if score_poll < 60 or score_poll > 1440:
            raise ValueError("MULTISPORT_SCORE_POLL_MINUTES must be between 60 and 1440")

        mma_result_poll = int(value("MULTISPORT_MMA_RESULT_POLL_MINUTES", "360"))
        if mma_result_poll < 60 or mma_result_poll > 1440:
            raise ValueError(
                "MULTISPORT_MMA_RESULT_POLL_MINUTES must be between 60 and 1440"
            )

        participant_refresh = int(value("MULTISPORT_PARTICIPANT_REFRESH_HOURS", "24"))
        if participant_refresh < 1 or participant_refresh > 168:
            raise ValueError("MULTISPORT_PARTICIPANT_REFRESH_HOURS must be between 1 and 168")

        history_lookback = int(value("MULTISPORT_HISTORY_LOOKBACK_DAYS", "3"))
        if history_lookback < 1 or history_lookback > 31:
            raise ValueError("MULTISPORT_HISTORY_LOOKBACK_DAYS must be between 1 and 31")

        history_requests = int(value("MULTISPORT_HISTORY_MAX_REQUESTS", "30"))
        if history_requests < 1 or history_requests > 200:
            raise ValueError("MULTISPORT_HISTORY_MAX_REQUESTS must be between 1 and 200")

        history_leagues = int(value("MULTISPORT_HISTORY_MAX_LEAGUES_PER_SPORT", "5"))
        if history_leagues < 1 or history_leagues > 25:
            raise ValueError(
                "MULTISPORT_HISTORY_MAX_LEAGUES_PER_SPORT must be between 1 and 25"
            )

        history_odds_events = int(value("MULTISPORT_HISTORY_MAX_ODDS_EVENTS", "10"))
        if history_odds_events < 0 or history_odds_events > 100:
            raise ValueError("MULTISPORT_HISTORY_MAX_ODDS_EVENTS must be between 0 and 100")

        research_requests = int(value("MULTISPORT_RESEARCH_MAX_REQUESTS", "30"))
        if research_requests < 1 or research_requests > 30:
            raise ValueError("MULTISPORT_RESEARCH_MAX_REQUESTS must be between 1 and 30")

        research_horizon = int(value("MULTISPORT_RESEARCH_HORIZON_MINUTES", "360"))
        if research_horizon < 30 or research_horizon > 1440:
            raise ValueError(
                "MULTISPORT_RESEARCH_HORIZON_MINUTES must be between 30 and 1440"
            )

        primary_book_profile=value('PRIMARY_BOOKMAKER_KEY')
        if primary_book_profile not in {'','hardrockbet','hardrockbet_fl','hardrockbet_az','hardrockbet_oh'}:
            raise ValueError('Unsupported primary bookmaker profile')
        def book_keys(name):
            return tuple(dict.fromkeys(primary_book_profile if b=='hardrockbet' and primary_book_profile else b for b in _csv(value(name))))

        raw_key = value("THE_ODDS_API_KEY")
        raw_therundown_key = value("THERUNDOWN_API_KEY")
        return cls(
            database_path=Path(value("DATABASE_PATH", "var/sports_analytics.sqlite3")),
            bot_mode=mode,
            odds_api_key=raw_key or None,
            therundown_api_key=raw_therundown_key or None,
            oddspapi_api_key=value("ODDSPAPI_API_KEY") or None,
            odds_api_io_key=value("ODDS_API_IO_KEY") or None,
            sports_game_odds_api_key=value("SPORTSGAMEODDS_API_KEY") or None,
            api_sports_key=value("API_SPORTS_KEY") or None,
            cfbd_api_key=value("CFBD_API_KEY") or None,
            nhl_cfb_odds_api_key=value("NHL_CFB_ODDS_API_KEY") or None,
            cfbd_2_api_key=value("CFBD_2_API_KEY") or None,
            football_data_api_key=value("FOOTBALL_DATA_API_KEY") or None,
            market_consensus_bookmakers=book_keys("MARKET_CONSENSUS_BOOKMAKERS"),
            accessible_bookmakers=book_keys("ACCESSIBLE_BOOKMAKERS"),
            odds_stale_after_minutes=stale_minutes,
            nws_user_agent=value(
                "NWS_USER_AGENT",
                "SportsAnalyticsPipeline/0.1 (contact: replace-with-real-contact)",
            ),
            http_timeout_seconds=timeout_seconds,
            http_retry_attempts=retry_attempts,
            odds_api_min_remaining_credits=min_remaining,
            odds_api_daily_credit_cap=daily_credit_cap,
            multisport_odds_horizon_minutes=odds_horizon,
            multisport_odds_refresh_minutes=odds_refresh,
            multisport_score_poll_minutes=score_poll,
            multisport_mma_result_poll_minutes=mma_result_poll,
            multisport_participant_refresh_hours=participant_refresh,
            multisport_history_lookback_days=history_lookback,
            multisport_history_max_requests=history_requests,
            multisport_history_max_leagues_per_sport=history_leagues,
            multisport_history_max_odds_events=history_odds_events,
            odds_api_io_history_bookmakers=_csv(
                value("ODDS_API_IO_HISTORY_BOOKMAKERS")
            ),
            multisport_research_feed_urls=_json_string_map(
                value("MULTISPORT_RESEARCH_FEEDS_JSON"),
                setting_name="MULTISPORT_RESEARCH_FEEDS_JSON",
            ),
            multisport_research_feed_token=(
                value("MULTISPORT_RESEARCH_FEED_TOKEN") or None
            ),
            multisport_research_max_requests=research_requests,
            multisport_research_horizon_minutes=research_horizon,
        )

    def require_odds_api_key(self) -> str:
        if not self.odds_api_key:
            raise RuntimeError("THE_ODDS_API_KEY is not configured")
        return self.odds_api_key

    def require_therundown_api_key(self) -> str:
        if not self.therundown_api_key:
            raise RuntimeError("THERUNDOWN_API_KEY is not configured")
        return self.therundown_api_key

    def provider_api_keys(self) -> dict[str, str | None]:
        """Return provider keys for internal clients; callers must never log this mapping."""
        return {
            "the_odds_api": self.odds_api_key,
            "oddspapi": self.oddspapi_api_key,
            "odds_api_io": self.odds_api_io_key,
            "sports_game_odds": self.sports_game_odds_api_key,
            "api_sports": self.api_sports_key,
            "cfbd": self.cfbd_api_key,
            "football_data": self.football_data_api_key,
        }

    def require_provider_api_keys(
        self,
        provider_keys: tuple[str, ...] = REQUIRED_PROVIDER_KEYS,
    ) -> None:
        configured = self.provider_api_keys()
        unknown = tuple(key for key in provider_keys if key not in configured)
        if unknown:
            raise ValueError(f"Unknown provider keys: {', '.join(unknown)}")
        missing = tuple(key for key in provider_keys if not configured[key])
        if missing:
            raise RuntimeError(f"Missing required provider API keys: {', '.join(missing)}")

    def sportsbook_lists_approved(self) -> bool:
        return bool(self.market_consensus_bookmakers and self.accessible_bookmakers)

    def require_nws_user_agent(self) -> str:
        if "replace-with-real-contact" in self.nws_user_agent.casefold():
            raise RuntimeError(
                "NWS_USER_AGENT must contain a real website or contact email before live weather collection"
            )
        return self.nws_user_agent

    def safe_summary(self) -> dict[str, object]:
        """Return settings that are safe to display or log."""
        return {
            "database_path": str(self.database_path),
            "bot_mode": self.bot_mode,
            "odds_api_key_configured": bool(self.odds_api_key),
            "therundown_api_key_configured": bool(self.therundown_api_key),
            "oddspapi_api_key_configured": bool(self.oddspapi_api_key),
            "odds_api_io_key_configured": bool(self.odds_api_io_key),
            "sports_game_odds_api_key_configured": bool(self.sports_game_odds_api_key),
            "api_sports_key_configured": bool(self.api_sports_key),
            "cfbd_api_key_configured": bool(self.cfbd_api_key),
            "football_data_api_key_configured": bool(self.football_data_api_key),
            "market_consensus_bookmakers": self.market_consensus_bookmakers,
            "accessible_bookmakers": self.accessible_bookmakers,
            "odds_stale_after_minutes": self.odds_stale_after_minutes,
            "odds_api_min_remaining_credits": self.odds_api_min_remaining_credits,
            "multisport_odds_horizon_minutes": self.multisport_odds_horizon_minutes,
            "multisport_participant_refresh_hours": self.multisport_participant_refresh_hours,
            "multisport_history_lookback_days": self.multisport_history_lookback_days,
            "multisport_history_max_requests": self.multisport_history_max_requests,
            "multisport_history_max_leagues_per_sport": (
                self.multisport_history_max_leagues_per_sport
            ),
            "multisport_history_max_odds_events": self.multisport_history_max_odds_events,
            "odds_api_io_history_bookmakers": self.odds_api_io_history_bookmakers,
            "multisport_research_feed_source_keys": tuple(
                sorted(self.multisport_research_feed_urls)
            ),
            "multisport_research_feed_token_configured": bool(
                self.multisport_research_feed_token
            ),
            "multisport_research_max_requests": self.multisport_research_max_requests,
            "multisport_research_horizon_minutes": self.multisport_research_horizon_minutes,
            # The user agent can contain a private contact email. Report only
            # whether a real identifying contact has been configured.
            "nws_user_agent_configured": bool(self.nws_user_agent)
            and "replace-with-real-contact" not in self.nws_user_agent.casefold(),
            "http_timeout_seconds": self.http_timeout_seconds,
            "http_retry_attempts": self.http_retry_attempts,
        }

