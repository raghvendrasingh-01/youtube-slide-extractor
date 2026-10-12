"""Typed application configuration.

The backend is intentionally local-first.  All persistent state lives below
``jobs_dir`` and local media is accepted only from explicit authorized roots
or a job's private work directory while that job is running.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _path_list(value: Any) -> list[Path]:
    """Accept a settings list, a path-separator string, or a comma list."""

    if value is None or value == "":
        return []
    if isinstance(value, (str, Path)):
        text = str(value)
        if text.lstrip().startswith("["):
            return [Path(item).expanduser() for item in json.loads(text)]
        # A path separator is the least surprising format for environment
        # variables.  Commas are accepted as a convenience on all platforms.
        pieces = [piece.strip() for piece in text.replace(",", os.pathsep).split(os.pathsep)]
        return [Path(piece).expanduser() for piece in pieces if piece]
    return [Path(item).expanduser() for item in value]


class AppSettings(BaseSettings):
    """Runtime settings for the API and worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FRAMENOTES_",
        extra="ignore",
        case_sensitive=False,
        # authorized_roots deliberately accepts a path-separator string (for
        # example ``/media:/tmp``), not only JSON list syntax.
        enable_decoding=False,
        allow_inf_nan=False,
    )

    app_name: str = "FrameNotes AI"
    jobs_dir: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[1] / "data/jobs")
    # Local files are opt-in.  The worker adds a job's private work directory
    # separately when it is processing an upload; the application working
    # directory must never become an implicit trust root.
    authorized_roots: list[Path] = Field(default_factory=list)
    default_interval: float = Field(5.0, ge=1, le=60)
    default_start_time: float = Field(0.0, ge=0)
    default_max_frames: int = Field(500, ge=1, le=3000)
    worker_poll_seconds: float = Field(0.20, gt=0, le=10)
    source_download_timeout: int = Field(900, ge=1, le=7200)
    max_job_seconds: int = Field(3600, ge=1, le=86_400)
    max_video_seconds: int = Field(14_400, ge=1, le=86_400)
    max_source_bytes: int = Field(1 << 30, ge=1, le=16 << 30)
    max_job_bytes: int = Field(2 << 30, ge=1, le=32 << 30)
    max_total_frames: int = Field(6000, ge=1, le=100_000)
    playlist_max_items: int = Field(30, ge=1, le=2000)
    max_queued_jobs: int = Field(8, ge=1, le=10_000)
    max_concurrent_jobs: int = Field(2, ge=1, le=8)
    delete_wait_seconds: float = Field(10, gt=0, le=60)
    retention_hours: int = Field(24, ge=1, le=8760)
    # The documented Docker/dev port is 3000; the local development launcher
    # may use 13000 when another app owns 3000.
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:13000",
            "http://127.0.0.1:13000",
        ]
    )
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "testserver"])
    session_secret: str | None = Field(default=None, repr=False, exclude=True)
    session_secret_path: Path | None = Field(default=None, repr=False, exclude=True)
    secure_cookies: bool = False
    # Singular spelling is accepted for deployments that expose one cookie
    # policy flag; secure_cookies remains the canonical setting.
    secure_cookie: bool | None = Field(default=None, repr=False)
    session_max_age_seconds: int = Field(30 * 86_400, ge=3600, le=365 * 86_400)
    max_request_body_bytes: int = Field(2 << 20, ge=1, le=64 << 20)
    max_session_expensive_requests: int = Field(30, ge=1, le=10_000)
    session_rate_window_seconds: int = Field(60, ge=1, le=86_400)
    max_session_jobs: int = Field(32, ge=1, le=10_000)
    max_stored_jobs: int = Field(128, ge=1, le=10_000)
    max_storage_bytes: int = Field(8 << 30, ge=1, le=512 << 30)
    max_concurrent_analyses: int = Field(2, ge=1, le=8)
    upload_timeout_seconds: float = Field(900, gt=0, le=7200)
    max_concurrent_exports: int = Field(1, ge=1, le=8)
    max_queued_exports: int = Field(4, ge=1, le=100)
    max_exports_per_job: int = Field(8, ge=1, le=100)
    max_export_bytes: int = Field(1 << 30, ge=1, le=32 << 30)
    cleanup_interval_seconds: float = Field(300, gt=0, le=86_400)
    shutdown_wait_seconds: float = Field(20, gt=0, le=120)

    @field_validator("cors_origins", mode="before")
    @classmethod
    def validate_origins(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return json.loads(value) if value.lstrip().startswith("[") else [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, value: list[str]) -> list[str]:
        for origin in value:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or "*" in origin
            ):
                raise ValueError("CORS origins must be explicit HTTP(S) origins")
        return [origin.rstrip("/") for origin in value]

    @field_validator("trusted_hosts", mode="before")
    @classmethod
    def validate_trusted_hosts(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            return json.loads(value) if value.lstrip().startswith("[") else [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("trusted_hosts")
    @classmethod
    def explicit_hosts(cls, value: list[str]) -> list[str]:
        if not value or any("*" in host or "/" in host for host in value):
            raise ValueError("trusted_hosts must name explicit local/deployment hosts")
        return value

    @field_validator("jobs_dir", mode="before")
    @classmethod
    def validate_jobs_dir(cls, value: Any) -> Path:
        return Path(value).expanduser()

    @field_validator("session_secret_path", mode="before")
    @classmethod
    def validate_session_secret_path(cls, value: Any) -> Path | None:
        if value in (None, ""):
            return None
        return Path(value).expanduser()

    @field_validator("authorized_roots", mode="before")
    @classmethod
    def validate_authorized_roots(cls, value: Any) -> list[Path]:
        return _path_list(value)

    @field_validator("default_interval")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("default_interval must be greater than zero")
        return value

    @field_validator("default_max_frames")
    @classmethod
    def validate_max_frames(cls, value: int) -> int:
        if value < 1:
            raise ValueError("default_max_frames must be at least one")
        return value

    def ensure_directories(self) -> None:
        """Create configured local directories if needed."""

        self.jobs_dir.expanduser().mkdir(parents=True, exist_ok=True)

    def normalized_authorized_roots(self) -> list[Path]:
        """Return absolute, explicitly configured local authorization roots."""

        return [root.expanduser().resolve(strict=False) for root in self.authorized_roots]
