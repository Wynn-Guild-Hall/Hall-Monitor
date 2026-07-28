"""Typed configuration loaded from environment variables (and ``.env``)."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    token: str = ""
    prefix: str = "~"
    discord_guild_id: int = 0
    welcome_channel_id: int = 0
    # The Current Guilds channel. Unset means the roster is never posted;
    # everything else still works.
    roster_channel_id: int = 0

    delegate_role_id: int = 0
    relegate_role_id: int = 0
    external_relegate_role_id: int = 0
    observer_role_id: int = 0
    janitor_role_id: int = 0
    monitor_role_id: int = 0
    ownership_contact_role_id: int = 0
    warring_contact_role_id: int = 0
    housing_contact_role_id: int = 0
    events_contact_role_id: int = 0

    hall_monitor_port: int = 9423
    data_dir: Path = Path("/app/data")

    wynncraft_api_base: str = "https://api.wynncraft.com"
    wynncraft_api_token: str = ""
    wynnpool_api_base: str = "https://api.wynnpool.com"
    mojang_api_base: str = "https://api.mojang.com"
    playerdb_api_base: str = "https://playerdb.co"
    athena_api_base: str = "https://athena.wynntils.com"

    pending_invite_ttl_minutes: int = 45
    pending_invite_sweep_seconds: int = 60
    notability_refresh_seconds: int = 3600

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "hall-monitor.db"

    @property
    def sqlite_url(self) -> str:
        return f"sqlite://{self.sqlite_path}"


settings = Settings()
