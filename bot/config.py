"""Centralized BETGPTAI configuration.

New v3 modules should read configuration from here instead of using
``os.environ`` directly. Legacy modules are still being migrated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def truthy(value: str | None) -> bool:
    """Return True for common environment truthy values."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime settings for local/Railway BETGPTAI deployments."""

    app_version: str = os.getenv("APP_VERSION", "BETGPTAI v3.0")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "")
    my_telegram_id: int = int(os.getenv("MY_TELEGRAM_ID", "594425739") or 594425739)
    app_timezone: str = os.getenv("APP_TIMEZONE", "America/New_York")
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    railway_environment: str = os.getenv("RAILWAY_ENVIRONMENT", "")
    railway_deployment_id: str = os.getenv("RAILWAY_DEPLOYMENT_ID", "")
    deployed_at: str = (
        os.getenv("DEPLOY_TIME")
        or os.getenv("RAILWAY_DEPLOYMENT_CREATED_AT")
        or os.getenv("RAILWAY_DEPLOYED_AT")
        or ""
    )
    local_bot_allowed: bool = truthy(os.getenv("LOCAL_BOT_ALLOWED"))
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    odds_api_key: str = os.getenv("ODDS_API_KEY", "")
    highlightly_api_key: str = os.getenv("HIGHLIGHTLY_API_KEY", "")
    football_data_api_key: str = os.getenv("FOOTBALL_DATA_API_KEY") or os.getenv("FOOTBALL_DATA_KEY", "")
    api_football_key: str = os.getenv("API_FOOTBALL_KEY", "")
    thesportsdb_api_key: str = os.getenv("THESPORTSDB_API_KEY") or os.getenv("THE_SPORTS_DB_API_KEY", "")
    free_channel_id: str = os.getenv("FREE_CHANNEL_ID", "")
    vip_channel_id: str = os.getenv("VIP_CHANNEL_ID", "")
    community_group_id: str = os.getenv("COMMUNITY_GROUP_ID", "")
    auto_post_approved: bool = truthy(os.getenv("AUTO_POST_APPROVED"))
    image_generation_enabled: bool = truthy(os.getenv("IMAGE_GENERATION_ENABLED"))

    @property
    def environment(self) -> str:
        """Return the normalized runtime environment."""
        return "railway" if self.railway_environment else "local"

    @property
    def timezone(self) -> ZoneInfo:
        """Return configured app timezone, falling back to America/New_York."""
        try:
            return ZoneInfo(self.app_timezone)
        except Exception:
            return ZoneInfo("America/New_York")


def get_settings() -> Settings:
    """Load a fresh immutable settings snapshot."""
    return Settings()
