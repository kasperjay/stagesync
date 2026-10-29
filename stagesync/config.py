"""StageSync Configuration Module.

Loads environment variables, manages credential lookup paths,
and provides type-safe configuration access with fallback for read-only environments.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from stagesync.security import enforce_file_permissions

logger = logging.getLogger(__name__)

# Automatically load .env if present and enforce 0600 permissions
_WORKSPACE_ENV = Path("/home/workspace/stagesync/.env")
if _WORKSPACE_ENV.exists():
    enforce_file_permissions(_WORKSPACE_ENV, 0o600)
    load_dotenv(_WORKSPACE_ENV)
else:
    load_dotenv()


def _resolve_default_config_dir() -> Path:
    """Resolve a writable configuration directory."""
    candidates = [
        Path("~/.config/stagesync").expanduser(),
        Path("/home/workspace/.config/stagesync"),
        Path("/tmp/stagesync/config"),
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            test_file = c / ".test_write"
            test_file.touch()
            test_file.unlink()
            return c
        except (OSError, PermissionError):
            continue
    return Path("/tmp/stagesync/config")


class Settings(BaseModel):
    """StageSync global application settings."""

    # Database & Storage
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", "sqlite:////home/workspace/stagesync/stagesync.db"
        )
    )
    cache_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("CACHE_DIR", "/home/workspace/stagesync/cache"))
    )

    # Google Photos Integration
    google_photos_client_id: Optional[str] = Field(
        default_factory=lambda: os.getenv("GOOGLE_PHOTOS_CLIENT_ID")
    )
    google_photos_client_secret: Optional[str] = Field(
        default_factory=lambda: os.getenv("GOOGLE_PHOTOS_CLIENT_SECRET")
    )
    google_photos_redirect_uri: str = Field(
        default_factory=lambda: os.getenv(
            "GOOGLE_PHOTOS_REDIRECT_URI", "http://localhost:8090/callback"
        )
    )
    google_photos_credentials_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "GOOGLE_PHOTOS_CREDENTIALS_PATH",
                str(_resolve_default_config_dir() / "google_photos_credentials.json"),
            )
        ).expanduser()
    )
    google_photos_token_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "GOOGLE_PHOTOS_TOKEN_PATH",
                str(_resolve_default_config_dir() / "google_photos_token.json"),
            )
        ).expanduser()
    )

    # Google Drive Integration
    google_drive_client_id: Optional[str] = Field(
        default_factory=lambda: os.getenv(
            "GOOGLE_DRIVE_CLIENT_ID",
            os.getenv("YOUTUBE_CLIENT_ID", os.getenv("GOOGLE_PHOTOS_CLIENT_ID")),
        )
    )
    google_drive_client_secret: Optional[str] = Field(
        default_factory=lambda: os.getenv(
            "GOOGLE_DRIVE_CLIENT_SECRET",
            os.getenv("YOUTUBE_CLIENT_SECRET", os.getenv("GOOGLE_PHOTOS_CLIENT_SECRET")),
        )
    )
    google_drive_redirect_uri: str = Field(
        default_factory=lambda: os.getenv(
            "GOOGLE_DRIVE_REDIRECT_URI",
            "http://localhost:8090/drive_callback",
        )
    )
    google_drive_credentials_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "GOOGLE_DRIVE_CREDENTIALS_PATH",
                str(_resolve_default_config_dir() / "google_drive_credentials.json"),
            )
        ).expanduser()
    )
    google_drive_token_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "GOOGLE_DRIVE_TOKEN_PATH",
                str(_resolve_default_config_dir() / "google_drive_token.json"),
            )
        ).expanduser()
    )

    # YouTube Data API v3 Integration
    youtube_client_id: Optional[str] = Field(
        default_factory=lambda: os.getenv("YOUTUBE_CLIENT_ID")
    )
    youtube_client_secret: Optional[str] = Field(
        default_factory=lambda: os.getenv("YOUTUBE_CLIENT_SECRET")
    )
    youtube_redirect_uri: str = Field(
        default_factory=lambda: os.getenv(
            "YOUTUBE_REDIRECT_URI", "http://localhost:8090/youtube_callback"
        )
    )
    youtube_credentials_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "YOUTUBE_CREDENTIALS_PATH",
                str(_resolve_default_config_dir() / "youtube_credentials.json"),
            )
        ).expanduser()
    )
    youtube_token_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "YOUTUBE_TOKEN_PATH",
                str(_resolve_default_config_dir() / "youtube_token.json"),
            )
        ).expanduser()
    )

    # External Intelligence APIs
    setlist_fm_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("SETLIST_FM_API_KEY")
    )
    gemini_api_key: Optional[str] = Field(
        default_factory=lambda: os.getenv("GEMINI_API_KEY")
    )
    gemini_model: str = Field(
        default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    )

    # Pipeline Policies
    default_privacy: str = Field(
        default_factory=lambda: os.getenv("STAGESYNC_DEFAULT_PRIVACY", "unlisted")
    )
    auto_approve_threshold: float = Field(
        default_factory=lambda: float(
            os.getenv("STAGESYNC_AUTO_APPROVE_THRESHOLD", "0.92")
        )
    )
    max_daily_uploads: int = Field(
        default_factory=lambda: int(os.getenv("STAGESYNC_MAX_DAILY_UPLOADS", "5"))
    )
    min_duration_seconds: float = Field(
        default_factory=lambda: float(os.getenv("STAGESYNC_MIN_DURATION_SECONDS", "8.0"))
    )

    @property
    def database_path(self) -> Path:
        """Extract Path from sqlite:/// URL or return direct path."""
        url = self.database_url
        if url.startswith("sqlite:///"):
            raw_path = url[len("sqlite:///") :]
            if url.startswith("sqlite:////"):
                return Path("/" + url[len("sqlite:////") :])
            return Path(raw_path)
        return Path(url)

    def ensure_directories(self) -> None:
        """Create required directories on filesystem with graceful fallback."""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.cache_dir = Path("/tmp/stagesync")
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        if str(self.database_path) != ":memory:":
            try:
                self.database_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                logger.debug("Database directory could not be created: %s", err)

        for p in [self.google_photos_token_path, self.youtube_token_path]:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                logger.debug("Config parent directory could not be created for %s: %s", p, err)


_SETTINGS_INSTANCE: Optional[Settings] = None


def get_settings(reload: bool = False) -> Settings:
    """Retrieve or initialize singleton Settings instance."""
    global _SETTINGS_INSTANCE
    if reload or _SETTINGS_INSTANCE is None:
        if _WORKSPACE_ENV.exists():
            load_dotenv(_WORKSPACE_ENV, override=True)
        _SETTINGS_INSTANCE = Settings()
        _SETTINGS_INSTANCE.ensure_directories()
    return _SETTINGS_INSTANCE


def reload_settings() -> Settings:
    """Force reload settings from environment and disk."""
    return get_settings(reload=True)
