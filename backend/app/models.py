"""Pydantic request, response, and persisted metadata models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
import math
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator


JobStatus = Literal["queued", "running", "cancelling", "exporting", "completed", "cancelled", "failed"]
DedupeMode = Literal["strict", "balanced", "aggressive", "manual", "off"]
DeduplicationMode = DedupeMode
ExportFormat = Literal["pdf", "pptx"]
ExportLayout = Literal["grid", "single", "filmstrip", "two", "four", "contact", "notes"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _split_sources(value: object) -> list[str]:
    """Flatten a string/list request into clean, de-duplicated lines.

    The URL parser performs semantic canonicalisation later.  Keeping this
    first pass small also means a request can contain a pasted multi-line
    playlist or a mixture of local paths and URLs.
    """

    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, (list, tuple, set)):
        values = []
        items = sorted(value, key=str) if isinstance(value, set) else value
        for item in items:
            values.extend(str(item).splitlines())
    elif value is None:
        values = []
    else:
        values = [str(value)]
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        cleaned = item.strip()
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        if cleaned and cleaned not in seen:
            result.append(cleaned)
            seen.add(cleaned)
    return result


def _seconds(value: object, *, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = str(value).strip()
        pieces = text.split(":")
        try:
            if len(pieces) == 1:
                result = float(pieces[0])
            elif len(pieces) == 2:
                result = float(pieces[0]) * 60 + float(pieces[1])
            elif len(pieces) == 3:
                result = float(pieces[0]) * 3600 + float(pieces[1]) * 60 + float(pieces[2])
            else:
                raise ValueError
        except ValueError as exc:
            raise ValueError(f"invalid time value: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError("time values must be finite")
    return result


class JobCreateRequest(BaseModel):
    """A job request accepted by ``POST /api/v1/jobs``."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    sources: list[str] = Field(default_factory=list, description="Authorized local paths or HTTPS YouTube URLs. For raw uploads use ['upload']; the server owns the upload path.")
    interval: float = Field(default=5.0, ge=1, le=60, description="Seconds between sampled frames")
    start_time: float = Field(default=0.0, ge=0, description="First timestamp to sample")
    end_time: float | None = Field(default=None, ge=0, description="Optional exclusive end timestamp")
    max_frames: int = Field(default=500, ge=1, le=3000, description="Candidate cap per video")
    dedupe_mode: DeduplicationMode = "balanced"
    ocr_enabled: bool = False
    phash_distance: int = Field(default=6, ge=0, le=20)
    include_first: bool = True
    include_last: bool = False
    scene_threshold: float = Field(default=0, ge=0, le=1)
    title: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def accept_common_source_aliases(cls, value: object) -> object:
        if isinstance(value, str):
            return {"sources": value}
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "sources" not in data:
            for alias in ("urls", "url", "source", "input", "inputs"):
                if alias in data:
                    data["sources"] = data.pop(alias)
                    break
        config = data.pop("config", None)
        if isinstance(config, dict):
            aliases = {
                "intervalSeconds": "interval", "startTime": "start_time",
                "endTime": "end_time", "maxFrames": "max_frames",
                "dedupeMode": "dedupe_mode", "ocrEnabled": "ocr_enabled",
            }
            for source_key, target_key in aliases.items():
                if target_key not in data and source_key in config:
                    data[target_key] = config[source_key]
        direct_aliases = {
            "intervalSeconds": "interval", "interval_seconds": "interval",
            "sampling_interval": "interval", "startTime": "start_time",
            "start": "start_time", "endTime": "end_time", "end": "end_time",
            "maxFrames": "max_frames", "dedupeMode": "dedupe_mode",
            "deduplication_mode": "dedupe_mode", "ocrEnabled": "ocr_enabled",
            "ocr": "ocr_enabled",
        }
        for source_key, target_key in direct_aliases.items():
            if target_key not in data and source_key in data:
                data[target_key] = data.pop(source_key)
        if isinstance(data.get("start_time"), str):
            data["start_time"] = _seconds(data["start_time"])
        if isinstance(data.get("end_time"), str):
            data["end_time"] = _seconds(data["end_time"]) if data["end_time"].strip() else None
        mode_map = {"perceptual": "balanced", "exact": "manual", "keep-all": "off"}
        if data.get("dedupe_mode") in mode_map:
            data["dedupe_mode"] = mode_map[data["dedupe_mode"]]
        return data

    @field_validator("sources", mode="before")
    @classmethod
    def normalize_sources(cls, value: object) -> list[str]:
        return _split_sources(value)

    @model_validator(mode="after")
    def validate_time_range(self) -> "JobCreateRequest":
        if not self.sources:
            raise ValueError("at least one source is required")
        if len(self.sources) > 200:
            raise ValueError("at most 200 source lines are supported per job")
        if self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        return self


class SourceRecord(BaseModel):
    """Resolved source state shown in job responses."""

    id: str
    input: str
    canonical: str
    kind: Literal["local", "youtube_video", "youtube_playlist", "remote"]
    title: str
    status: Literal["queued", "running", "completed", "failed", "cancelled", "skipped"] = "queued"
    error: str | None = None
    frame_count: int = 0
    video_id: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    channel: str | None = None
    playlist_count: int | None = None
    position: int = 0
    exact_removed: int = 0
    near_removed: int = 0
    retained_count: int = 0
    processing_seconds: float = 0


class FrameMetadata(BaseModel):
    """Stable metadata for an extracted frame."""

    model_config = ConfigDict(extra="ignore")

    id: str
    source_id: str
    source_title: str
    index: int = Field(ge=0)
    timestamp: float = Field(ge=0)
    path: str = Field(description="Job-relative frame artifact path; API responses contain only its basename")
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    exact_hash: str | None = None
    perceptual_hash: str | None = None
    retained: bool = True
    duplicate_of: str | None = None
    matched_frame_id: str | None = None
    duplicate_reason: Literal["exact_hash", "perceptual_hash", "ssim"] | None = None
    duplicate_similarity: float | None = None
    ocr_text: str | None = None
    image_url: str | None = None
    retention_overridden: bool = False
    review_order: int | None = None
    thumbnail_url: str | None = None

    @property
    def phash(self) -> str | None:
        """Short alias useful to callers familiar with imagehash."""

        return self.perceptual_hash


class DownloadArtifact(BaseModel):
    """An export that can be downloaded from the API."""

    filename: str
    format: ExportFormat
    size_bytes: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    download_url: str | None = None
    page_count: int = 0
    source_id: str | None = None

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            Path(value).name != value
            or value in {"", ".", ".."}
            or ".tmp." in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("artifact filename must be a safe published basename")
        return value


class JobRecord(BaseModel):
    """Persisted job document and the API's canonical job representation."""

    model_config = ConfigDict(extra="ignore")

    id: str
    # The owner is persisted for authorization but excluded from the public
    # model dump.  ``JobStore`` explicitly adds it to durable JSON snapshots.
    owner_id: str = Field(default="", exclude=True, repr=False)
    status: JobStatus = "queued"
    progress: float = Field(default=0.0, ge=0, le=100)
    stage: str = "queued"
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    request: JobCreateRequest
    sources: list[SourceRecord] = Field(default_factory=list)
    frames: list[FrameMetadata] = Field(default_factory=list)
    downloads: list[DownloadArtifact] = Field(default_factory=list)
    ocr_status: str = "disabled"
    warnings: list[str] = Field(default_factory=list)
    techniques: list[str] = Field(default_factory=lambda: ["pixel SHA-256", "pHash", "SSIM", "changed-pixel guard"])
    processing_seconds: float = 0
    expires_at: datetime | None = None
    export_count: int = Field(default=0, ge=0, exclude=True, repr=False)
    export_bytes: int = Field(default=0, ge=0, exclude=True, repr=False)

    @computed_field
    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @computed_field
    @property
    def duplicate_count(self) -> int:
        return sum(1 for frame in self.frames if frame.duplicate_of)

    @computed_field
    @property
    def retained_count(self) -> int:
        return sum(frame.retained for frame in self.frames)


class JobSummary(BaseModel):
    """Small response shape available to clients that do not need frames."""

    id: str
    status: JobStatus
    progress: float
    stage: str
    error: str | None = None
    frame_count: int = 0
    duplicate_count: int = 0
    retained_count: int = 0
    ocr_status: str = "disabled"
    downloads: list[DownloadArtifact] = Field(default_factory=list)


class SessionResponse(BaseModel):
    """Session is returned only as an HttpOnly, SameSite=Lax cookie."""

    ok: Literal[True] = True


class ExportAcceptedResponse(BaseModel):
    """Rendering is queued. Follow job progress/SSE and fetch downloads after 'exported'."""

    job_id: str
    status: Literal["exporting"] = "exporting"
    downloads: list[DownloadArtifact] = Field(default_factory=list)


class FrameListResponse(BaseModel):
    frames: list[FrameMetadata]
    total: int
    offset: int
    limit: int
    next_cursor: str | None = None


class FrameRetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    retained: bool = Field(alias="keep")

    @model_validator(mode="before")
    @classmethod
    def accept_status(cls, value: object) -> object:
        if isinstance(value, dict):
            data = dict(value)
            if "retained" not in data and "keep" not in data and "status" in data:
                status = str(data.pop("status")).lower()
                if status not in {"retained", "discarded"}:
                    raise ValueError("status must be retained or discarded")
                data["retained"] = status == "retained"
            return data
        return value


class FrameOrderRequest(BaseModel):
    frame_ids: list[str] = Field(min_length=1, max_length=10000)


class BulkRetentionRequest(BaseModel):
    frame_ids: list[str] = Field(min_length=1, max_length=10000)
    status: Literal["retained", "discarded"]


class AnalyzeRequest(BaseModel):
    sources: list[str] = Field(min_length=1, max_length=30)

    @field_validator("sources", mode="before")
    @classmethod
    def normalize_sources(cls, value: object) -> list[str]:
        return _split_sources(value)


class ExportRequest(BaseModel):
    """Options for PDF/PPTX export."""

    model_config = ConfigDict(extra="forbid")

    formats: list[ExportFormat] = Field(default_factory=lambda: ["pdf"])
    images_only: bool = False
    layout: ExportLayout = "single"
    columns: int = Field(default=2, ge=1, le=6)
    include_cover: bool = True
    include_timestamps: bool = True
    include_source_titles: bool = True
    title: str = Field(default="FrameNotes AI", min_length=1, max_length=200)
    include_ocr_text: bool = False
    include_sections: bool = True
    separate_videos: bool = False
    page_size: Literal["A4", "Letter"] = "A4"
    orientation: Literal["portrait", "landscape"] = "portrait"
    margin_mm: int = Field(default=12, ge=5, le=30)
    aspect_ratio: Literal["16:9", "4:3"] = "16:9"
    footer: str = Field(default="FrameNotes AI", max_length=100)

    @model_validator(mode="before")
    @classmethod
    def accept_format_aliases(cls, value: object) -> object:
        if isinstance(value, str):
            return {"formats": [value]}
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if isinstance(data.get("config"), dict):
            nested = data.pop("config")
            data = {**nested, **data}
        export_aliases = {
            "exportFormats": "formats", "includeCover": "include_cover",
            "includeTimestamps": "include_timestamps", "includeSourceTitles": "include_source_titles",
            "imagesOnly": "images_only",
        }
        for source_key, target_key in export_aliases.items():
            if target_key not in data and source_key in data:
                data[target_key] = data.pop(source_key)
        if "formats" not in data:
            if "format" in data:
                data["formats"] = [data.pop("format")]
            elif "output_format" in data:
                data["formats"] = [data.pop("output_format")]
        return data

    @field_validator("layout", mode="before")
    @classmethod
    def normalize_layout(cls, value: object) -> object:
        return {"contact-sheet": "grid", "slides": "single"}.get(str(value).lower(), value)

    @field_validator("formats", mode="before")
    @classmethod
    def normalize_formats(cls, value: object) -> list[str]:
        if isinstance(value, str):
            value = [value]
        values = [str(item).lower().strip() for item in (value or [])]
        if not values:
            raise ValueError("at least one export format is required")
        allowed = {"pdf", "pptx"}
        invalid = sorted(set(values) - allowed)
        if invalid:
            raise ValueError(f"unsupported export format(s): {', '.join(invalid)}")
        return list(dict.fromkeys(values))


class ProgressEvent(BaseModel):
    type: Literal["progress", "stage", "complete", "error", "cancelled"] = "progress"
    job_id: str
    status: JobStatus
    progress: float
    stage: str
    frame_count: int
    duplicate_count: int
    retained_count: int = 0
    ocr_status: str = "disabled"
    error: str | None = None
    current_video: str | None = None
    total_videos: int = 0
    completed_videos: int = 0
    warnings: list[str] = Field(default_factory=list)
