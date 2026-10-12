"""Local/YouTube media access and bounded, timestamp-aware frame extraction."""

from __future__ import annotations

import importlib
import importlib.util
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import numpy as np
from PIL import Image

from .errors import CancelledProcessing, MediaExtractionError, SourceAccessError
from .processes import process_error, run_process
from .urls import ParsedSource, YOUTUBE_HOSTS


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".mpeg",
    ".mpg",
    ".wmv",
}
MAX_FRAME_DIMENSION = 1920
MAX_THUMBNAIL_DIMENSION = 480
_TRANSIENT_MARKERS = (
    "timed out",
    "timeout",
    "temporarily unavailable",
    "connection reset",
    "connection aborted",
    "connection refused",
    "network is unreachable",
    "name resolution",
    "http error 408",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "408 request timeout",
    "429 too many requests",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "server error",
)
_PERMANENT_MARKERS = (
    "private",
    "sign in",
    "authentication",
    "login",
    "members-only",
    "drm",
    "copyright",
    "this video is unavailable",
    "video unavailable",
    "requested format is not available",
    "unavailable in your country",
    "age-restricted",
    "premium",
    "subscriber",
)


@dataclass(frozen=True)
class ExtractionConfig:
    interval: float
    start_time: float = 0.0
    end_time: float | None = None
    max_frames: int = 500

    def __post_init__(self) -> None:
        if not all(
            math.isfinite(value)
            for value in (self.interval, self.start_time, self.end_time if self.end_time is not None else 0)
        ):
            raise ValueError("sampling times must be finite")
        if self.interval <= 0:
            raise ValueError("interval must be greater than zero")
        if self.start_time < 0:
            raise ValueError("start_time cannot be negative")
        if self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("end_time must be greater than start_time")
        if self.max_frames < 1:
            raise ValueError("max_frames must be at least one")


@dataclass(frozen=True)
class CapturedFrame:
    index: int
    timestamp: float
    path: Path
    width: int
    height: int


def _cv2():
    try:
        return importlib.import_module("cv2")
    except ImportError as exc:
        raise MediaExtractionError(
            "OpenCV is required for video extraction. Install backend/requirements.txt."
        ) from exc


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise MediaExtractionError("job exceeded its maximum wall-clock time")


def _remaining_timeout(deadline: float | None, fallback: float) -> float:
    if deadline is None:
        return max(0.1, float(fallback))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise MediaExtractionError("job exceeded its maximum wall-clock time")
    return max(0.1, min(float(fallback), remaining))


def check_local_authorized(
    path: Path | str,
    authorized_roots: Iterable[Path | str],
    job_work_root: Path | str | None = None,
    max_bytes: int | None = None,
) -> Path:
    """Resolve a local file below an explicit root or this job's work root.

    ``resolve`` is intentionally done before the containment check so a
    symlink cannot escape an authorized directory.  The job work root is
    supplied by the pipeline, never inferred from user input.
    """

    candidate = Path(path).expanduser().resolve(strict=False)
    if not candidate.exists():
        raise SourceAccessError(f"Local source does not exist: {path}")
    if not candidate.is_file():
        raise SourceAccessError(f"Local source is not a file: {path}")
    roots = [Path(root).expanduser().resolve(strict=False) for root in authorized_roots]
    if job_work_root is not None:
        roots.append(Path(job_work_root).expanduser().resolve(strict=False))
    allowed = False
    for root in roots:
        try:
            candidate.relative_to(root)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        roots_text = ", ".join(str(root) for root in roots) or "(none)"
        raise SourceAccessError(
            f"Local source is outside authorized folders: {candidate}. Allowed roots: {roots_text}"
        )
    if max_bytes is not None:
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise SourceAccessError(f"Could not inspect local source size: {candidate}") from exc
        if size > max_bytes:
            raise SourceAccessError(
                f"Local source exceeds the {max_bytes} byte source limit: {candidate}"
            )
    return candidate


def _is_youtube_video(source: ParsedSource) -> bool:
    return (
        source.kind == "youtube_video"
        and source.video_id is not None
        and len(source.video_id) == 11
        and all(character.isalnum() or character in "-_" for character in source.video_id)
        and source.canonical == f"https://www.youtube.com/watch?v={source.video_id}"
    )


def _transient_error(error: Exception) -> bool:
    message = process_error(error).lower()
    if any(marker in message for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def _run_with_transient_retries(
    command: list[str],
    *,
    cancel_event: Event | None,
    timeout: float,
    attempts: int = 2,
) -> subprocess.CompletedProcess[str]:
    """Retry only errors that plausibly represent temporary transport faults."""

    last_error: Exception | None = None
    started = time.monotonic()
    for attempt in range(max(1, attempts)):
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            return run_process(command, cancel_event=cancel_event, timeout=remaining)
        except CancelledProcessing:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            last_error = exc
            if attempt + 1 >= max(1, attempts) or not _transient_error(exc):
                raise
            if cancel_event is not None and cancel_event.wait(0.1):
                raise CancelledProcessing("Job cancelled while retrying a temporary source failure")
    assert last_error is not None
    raise last_error


def _ytdlp_cli_json(
    source: ParsedSource,
    *,
    cancel_event: Event | None,
    timeout: float,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-cookies",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout",
        "15",
        "--retries",
        "0",
        "--fragment-retries",
        "0",
        "--extractor-retries",
        "0",
        "--use-extractors",
        "youtube",
         "--format",
         "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]/bestvideo[ext=mp4][height<=1080]/best[ext=mp4]/best",
        "--",
        source.canonical,
    ]
    result = _run_with_transient_retries(
        command,
        cancel_event=cancel_event,
        timeout=max(1.0, timeout),
    )
    try:
        info = json.loads(result.stdout)
    except (TypeError, ValueError) as exc:
        raise SourceAccessError("yt-dlp returned invalid metadata") from exc
    if not isinstance(info, dict):
        raise SourceAccessError("yt-dlp returned an invalid metadata object")
    return info


def _ytdlp_module_info(source: ParsedSource, module: Any, options: dict[str, Any]) -> dict[str, Any]:
    try:
        with module.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source.canonical, download=False)
    except Exception as exc:
        raise SourceAccessError(
            f"Could not access or inspect source {source.canonical}: {process_error(exc)}"
        ) from exc
    if not isinstance(info, dict):
        raise SourceAccessError("yt-dlp returned an invalid metadata object")
    return info


def extract_remote_metadata(
    source: ParsedSource,
    *,
    cancel_event: Event | None = None,
    timeout: float = 120,
    yt_dlp_module: Any = None,
) -> dict[str, Any]:
    """Fetch YouTube metadata without downloading media or using credentials."""

    if not _is_youtube_video(source):
        raise SourceAccessError("only canonical YouTube video sources can be inspected")
    if source.error:
        raise SourceAccessError(source.error)
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("Job cancelled before source metadata lookup")
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": min(max(1, int(timeout)), 15),
        # CDN URLs can expire between metadata discovery and the media
        # request. Let yt-dlp refresh a transient 403 once without retrying
        # permanent availability/authentication failures at the job layer.
        "retries": 1,
        "fragment_retries": 1,
        "extractor_retries": 1,
        "allowed_extractors": ["youtube"],
        "cachedir": False,
    }
    try:
        if yt_dlp_module is not None and cancel_event is None:
            info = _ytdlp_module_info(source, yt_dlp_module, options)
        else:
            if importlib.util.find_spec("yt_dlp") is None:
                raise SourceAccessError("yt-dlp is not installed")
            info = _ytdlp_cli_json(source, cancel_event=cancel_event, timeout=min(timeout, 120))
    except CancelledProcessing:
        raise
    except SourceAccessError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceAccessError(
            f"Could not access or inspect source {source.canonical}: {process_error(exc)}"
        ) from exc

    returned_id = str(info.get("id") or "")
    if returned_id and returned_id != source.video_id:
        raise SourceAccessError("YouTube extractor returned a different video ID")
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if extractor and "youtube" not in extractor:
        raise SourceAccessError("source was not handled by the YouTube extractor")
    for key in ("webpage_url", "original_url"):
        returned_url = info.get(key)
        if not returned_url:
            continue
        parsed = urlparse(str(returned_url))
        try:
            port = parsed.port
        except ValueError as exc:
            raise SourceAccessError("YouTube metadata returned an invalid port") from exc
        if (
            parsed.scheme.lower() != "https"
            or (parsed.hostname or "").lower() not in YOUTUBE_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise SourceAccessError("YouTube metadata returned an unsafe redirect target")
    return info


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _metadata_video_size(info: dict[str, Any]) -> int | None:
    sizes: list[int] = []
    for key in ("filesize", "filesize_approx"):
        value = info.get(key)
        if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
            sizes.append(int(value))
    requested = info.get("requested_formats") or []
    if isinstance(requested, list):
        for fmt in requested:
            if not isinstance(fmt, dict) or fmt.get("vcodec") in {None, "none"}:
                continue
            for key in ("filesize", "filesize_approx"):
                value = fmt.get(key)
                if isinstance(value, (int, float)) and math.isfinite(float(value)) and value >= 0:
                    sizes.append(int(value))
    return max(sizes) if sizes else None


def _validate_remote_metadata(
    info: dict[str, Any],
    *,
    max_source_bytes: int,
    max_video_seconds: float,
) -> tuple[float, str | None]:
    availability = str(info.get("availability") or "").lower()
    if availability in {
        "private",
        "premium_only",
        "subscriber_only",
        "needs_auth",
        "login_required",
        "unavailable",
        "deleted",
    }:
        raise SourceAccessError(f"YouTube source is unavailable ({availability.replace('_', ' ')})")
    if info.get("has_drm"):
        raise SourceAccessError("DRM-protected YouTube sources are not supported")
    if info.get("is_live") or info.get("live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise SourceAccessError("Live streams are not supported")
    duration = _finite_number(info.get("duration"))
    if duration is None or duration <= 0:
        raise SourceAccessError("YouTube source has no usable duration")
    if duration > max_video_seconds:
        raise SourceAccessError(
            f"YouTube source duration {duration:g}s exceeds the {max_video_seconds:g}s limit"
        )
    size = _metadata_video_size(info)
    if size is not None and size > max_source_bytes:
        raise SourceAccessError(
            f"YouTube source is approximately {size} bytes, above the {max_source_bytes} byte limit"
        )
    title = str(info.get("title") or "") or None
    return duration, title


def _download_options(destination: Path, max_source_bytes: int) -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreconfig": True,
        "cachedir": False,
        "outtmpl": str(destination / "media.%(ext)s"),
        # Prefer a directly downloadable H.264/MP4 stream. Some public videos
        # advertise AV1 first; selecting it can produce an HTTP 403 or leave
        # OpenCV dependent on an unavailable hardware decoder even though a
        # permitted AVC stream is available in the same manifest.
        "format": "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]/bestvideo[ext=mp4][height<=1080]/best[ext=mp4]/best",
        "max_filesize": max_source_bytes,
        "socket_timeout": 15,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "allowed_extractors": ["youtube"],
        "restrictfilenames": True,
        "overwrites": False,
    }


def _media_candidates(destination: Path) -> list[Path]:
    return sorted(
        {
            candidate.resolve()
            for candidate in destination.glob("media.*")
            if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
        }
    )


def download_remote_source(
    source: ParsedSource,
    destination: Path,
    cancel_event: Event | None = None,
    timeout: int = 900,
    yt_dlp_module: Any = None,
    max_source_bytes: int = 1 << 30,
    max_video_seconds: int = 14_400,
    deadline: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[Path, str | None]:
    """Download one validated YouTube video as a fixed-name video-only file."""

    if not _is_youtube_video(source):
        raise SourceAccessError("only canonical HTTPS YouTube video URLs can be downloaded")
    if source.error:
        raise SourceAccessError(source.error)
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("job cancelled before remote download")
    destination = Path(destination).resolve(strict=False)
    destination.mkdir(parents=True, exist_ok=True)
    timeout = max(1, int(timeout))
    started = time.monotonic()
    try:
        info = metadata or extract_remote_metadata(
            source,
            cancel_event=cancel_event,
            timeout=_remaining_timeout(deadline, min(timeout, 120)),
            yt_dlp_module=yt_dlp_module if cancel_event is None else None,
        )
        _check_deadline(deadline)
        duration, title = _validate_remote_metadata(
            info,
            max_source_bytes=max_source_bytes,
            max_video_seconds=max_video_seconds,
        )
        if yt_dlp_module is not None and cancel_event is None:
            options = _download_options(destination, max_source_bytes)
            with yt_dlp_module.YoutubeDL(options) as ydl:
                ydl.extract_info(source.canonical, download=True)
        else:
            if importlib.util.find_spec("yt_dlp") is None:
                raise SourceAccessError(
                    f"Cannot download remote source {source.canonical}: yt-dlp is not installed."
                )
            command = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--ignore-config",
                "--no-cookies",
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--socket-timeout",
                "15",
                 "--retries",
                 "1",
                 "--fragment-retries",
                 "1",
                 "--extractor-retries",
                 "1",
                "--use-extractors",
                "youtube",
                 "--format",
                 "bestvideo[ext=mp4][vcodec^=avc1][height<=1080]/bestvideo[ext=mp4][height<=1080]/best[ext=mp4]/best",
                "--max-filesize",
                str(max_source_bytes),
                "--output",
                str(destination / "media.%(ext)s"),
                "--restrict-filenames",
                "--no-overwrites",
                "--",
                source.canonical,
            ]
            _run_with_transient_retries(
                command,
                cancel_event=cancel_event,
                timeout=_remaining_timeout(deadline, min(timeout, 900)),
            )
    except CancelledProcessing:
        raise
    except SourceAccessError:
        raise
    except Exception as exc:  # yt-dlp exposes several exception types
        raise SourceAccessError(
            f"Could not access or download source {source.canonical}: {process_error(exc)}"
        ) from exc

    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("job cancelled after remote download")
    candidates = _media_candidates(destination)
    if not candidates:
        raise SourceAccessError(
            f"yt-dlp reported success for {source.canonical}, but no media file was produced."
        )
    media = candidates[0]
    try:
        size = media.stat().st_size
    except OSError as exc:
        raise SourceAccessError(f"Could not inspect downloaded media {media}") from exc
    if size > max_source_bytes:
        media.unlink(missing_ok=True)
        raise SourceAccessError(f"Downloaded source exceeds the {max_source_bytes} byte limit")
    remaining_budget = timeout - (time.monotonic() - started)
    if remaining_budget <= 0 and deadline is None:
        media.unlink(missing_ok=True)
        raise SourceAccessError("remote download exceeded its bounded timeout")
    probed_duration = probe_media_duration(
        media,
        cancel_event=cancel_event,
        timeout=min(remaining_budget, 30),
        deadline=deadline,
    )
    if probed_duration is not None and probed_duration > max_video_seconds:
        media.unlink(missing_ok=True)
        raise SourceAccessError(
            f"Downloaded source duration {probed_duration:g}s exceeds the {max_video_seconds:g}s limit"
        )
    dimensions = probe_media_dimensions(
        media,
        cancel_event=cancel_event,
        timeout=min(max(0.1, remaining_budget), 30),
        deadline=deadline,
    )
    if dimensions is not None and max(dimensions) > MAX_FRAME_DIMENSION:
        media.unlink(missing_ok=True)
        raise SourceAccessError("downloaded video resolution exceeds the 1920 pixel limit")
    # ``duration`` came from the no-download metadata guard.  Retain the
    # value in the local variable so a mocked downloader cannot silently skip
    # the preflight check.
    _ = duration
    return media, title


def _resize_frame(frame: np.ndarray, cv2_module) -> np.ndarray:
    height, width = frame.shape[:2]
    largest = max(int(width), int(height))
    if largest <= MAX_FRAME_DIMENSION:
        return frame
    scale = MAX_FRAME_DIMENSION / largest
    return cv2_module.resize(
        frame,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2_module.INTER_AREA,
    )


def _save_frame(cv2_module, frame: np.ndarray, output: Path) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    bounded = _resize_frame(frame, cv2_module)
    # JPEG quality is fixed so identical source frames have deterministic
    # bytes and downstream exact hashing is stable across runs.
    ok = cv2_module.imwrite(str(output), bounded, [int(cv2_module.IMWRITE_JPEG_QUALITY), 95])
    if not ok:
        raise MediaExtractionError(f"Could not write extracted frame {output}")
    height, width = bounded.shape[:2]
    return int(width), int(height)


def _extract_image(
    source_path: Path,
    output_dir: Path,
    cancel_event: Event | None,
    deadline: float | None,
) -> list[CapturedFrame]:
    _check_deadline(deadline)
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("job cancelled before image extraction")
    try:
        with Image.open(source_path) as image:
            rgb = image.convert("RGB")
            array = np.asarray(rgb)
    except Exception as exc:
        raise MediaExtractionError(f"Could not open image source {source_path}: {exc}") from exc
    cv2_module = _cv2()
    bgr = cv2_module.cvtColor(array, cv2_module.COLOR_RGB2BGR)
    path = output_dir / "frame-000000.jpg"
    width, height = _save_frame(cv2_module, bgr, path)
    return [CapturedFrame(index=0, timestamp=0.0, path=path, width=width, height=height)]


def _ffprobe_frame_timestamps(
    source: Path,
    *,
    cancel_event: Event | None,
    deadline: float | None,
) -> list[float | None]:
    executable = shutil.which("ffprobe")
    if not executable:
        return []
    _check_deadline(deadline)
    try:
        result = run_process(
            [
                executable,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time,pkt_pts_time",
                "-of",
                "csv=p=0",
                str(source),
            ],
            cancel_event=cancel_event,
            timeout=_remaining_timeout(deadline, 30.0),
        )
    except CancelledProcessing:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    timestamps: list[float | None] = []
    for line in result.stdout.splitlines():
        timestamp: float | None = None
        for value in line.strip().split(","):
            try:
                candidate = float(value)
            except ValueError:
                continue
            if math.isfinite(candidate) and candidate >= 0:
                timestamp = candidate
                break
        timestamps.append(timestamp)
    return timestamps


def extract_frames(
    source_path: Path | str,
    output_dir: Path | str,
    config: ExtractionConfig,
    cancel_event: Event | None = None,
    on_frame: Callable[[CapturedFrame], None] | None = None,
    *,
    include_first: bool = True,
    include_last: bool = False,
    scene_threshold: float = 0.0,
    max_video_seconds: float | None = None,
    deadline: float | None = None,
) -> list[CapturedFrame]:
    """Extract timestamped frames with real PTS/FPS metadata and hard bounds."""

    source = Path(source_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    if not 0 <= scene_threshold <= 1 or not math.isfinite(scene_threshold):
        raise ValueError("scene_threshold must be between zero and one")
    _check_deadline(deadline)
    if source.suffix.lower() in IMAGE_EXTENSIONS:
        if config.start_time > 0 or (config.end_time is not None and config.end_time <= 0):
            raise MediaExtractionError(
                "No image frame is available in the requested time range (images have timestamp 0)"
            )
        frames = _extract_image(source, destination, cancel_event, deadline)
        if on_frame:
            on_frame(frames[0])
        return frames[: config.max_frames]

    duration = probe_media_duration(
        source,
        cancel_event=cancel_event,
        timeout=_remaining_timeout(deadline, 30.0),
    )
    if duration is not None:
        if duration < 0 or not math.isfinite(duration):
            raise MediaExtractionError(f"Video duration is invalid for {source}")
        if max_video_seconds is not None and duration > max_video_seconds:
            raise MediaExtractionError(
                f"Video duration {duration:g}s exceeds the {max_video_seconds:g}s limit"
            )
        if config.start_time >= duration and duration > 0:
            raise MediaExtractionError(f"No frames were available in {source} for the requested time range")
    elif max_video_seconds is not None:
        # A missing ffprobe duration is not a reason to invent a duration. The
        # decoder loop still enforces the cap once an observed PTS crosses it.
        pass

    cv2_module = _cv2()
    capture = cv2_module.VideoCapture(str(source))
    opened = capture.isOpened()
    capture.release()
    try:
        if not opened:
            raise MediaExtractionError(
                f"Could not decode video source {source}. Ensure ffmpeg/OpenCV supports its codec."
            )
        return _extract_opencv(
            source,
            destination,
            config,
            cancel_event,
            on_frame,
            include_first=include_first,
            include_last=include_last,
            scene_threshold=scene_threshold,
            max_video_seconds=max_video_seconds,
            deadline=deadline,
        )
    except MediaExtractionError as primary_error:
        # Some OpenCV builds report that an AV1/VP9 stream is open but fail on
        # the first read (for example when hardware acceleration is present but
        # unsupported). Retry through FFmpeg's software decoder in that case.
        # Do not repeat this fallback after partial callback output: the caller
        # already owns those frames and can report the partial source safely.
        if list(destination.glob("frame-*.jpg")):
            raise
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise
        normalized = destination / "normalized.avi"
        try:
            run_process(
                [
                    ffmpeg,
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-an",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "3",
                    str(normalized),
                ],
                cancel_event=cancel_event,
                timeout=_remaining_timeout(deadline, 900.0),
            )
            return _extract_opencv(
                normalized,
                destination,
                config,
                cancel_event,
                on_frame,
                include_first=include_first,
                include_last=include_last,
                scene_threshold=scene_threshold,
                max_video_seconds=max_video_seconds,
                deadline=deadline,
            )
        except (OSError, subprocess.SubprocessError, MediaExtractionError) as fallback_error:
            raise MediaExtractionError(
                f"Could not decode video source {source} with OpenCV or FFmpeg: "
                f"{process_error(fallback_error)}"
            ) from primary_error
        finally:
            normalized.unlink(missing_ok=True)


def _extract_opencv(
    source: Path,
    destination: Path,
    config: ExtractionConfig,
    cancel_event: Event | None,
    on_frame: Callable[[CapturedFrame], None] | None,
    *,
    include_first: bool,
    include_last: bool,
    scene_threshold: float,
    max_video_seconds: float | None,
    deadline: float | None,
) -> list[CapturedFrame]:
    cv2_module = _cv2()
    capture = cv2_module.VideoCapture(str(source))
    if not capture.isOpened():
        capture.release()
        raise MediaExtractionError(f"Could not decode video source {source}")
    reported_fps = float(capture.get(cv2_module.CAP_PROP_FPS) or 0.0)
    fps = reported_fps if math.isfinite(reported_fps) and reported_fps > 0 else None
    # ffprobe is the authoritative source for VFR PTS when available.  If it
    # cannot provide timestamps, OpenCV's declared FPS is used only when it is
    # real metadata; there is no fabricated 25 FPS fallback.
    probe_timestamps = _ffprobe_frame_timestamps(
        source,
        cancel_event=cancel_event,
        deadline=deadline,
    )

    result: list[CapturedFrame] = []
    frame_number = 0
    next_timestamp = float(config.start_time if include_first else config.start_time + config.interval)
    last_timestamp = -1.0
    last_frame: np.ndarray | None = None
    last_frame_number = -1
    last_frame_timestamp = -1.0
    previous_scene: np.ndarray | None = None
    try:
        while True:
            _check_deadline(deadline)
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledProcessing("job cancelled during frame extraction")
            ok, frame = capture.read()
            if not ok:
                break

            probe_timestamp = probe_timestamps[frame_number] if frame_number < len(probe_timestamps) else None
            reported_timestamp = float(capture.get(cv2_module.CAP_PROP_POS_MSEC)) / 1000
            timestamp: float | None = None
            if probe_timestamp is not None and probe_timestamp >= 0:
                timestamp = probe_timestamp
            elif math.isfinite(reported_timestamp) and reported_timestamp >= 0 and (
                frame_number == 0 or reported_timestamp > last_timestamp + 1e-7
            ):
                timestamp = reported_timestamp
            elif fps is not None:
                timestamp = frame_number / fps
            if timestamp is None or not math.isfinite(timestamp) or timestamp < 0:
                capture.release()
                raise MediaExtractionError(
                    f"Video source {source} does not expose reliable presentation timestamps or FPS metadata"
                )
            if timestamp + 1e-7 < last_timestamp:
                # Decoder timestamps should be monotonic; do not silently
                # reorder or fabricate them when a backend violates that.
                capture.release()
                raise MediaExtractionError(f"Video source {source} returned non-monotonic timestamps")
            last_timestamp = timestamp
            frame_number += 1

            if max_video_seconds is not None and timestamp > max_video_seconds:
                capture.release()
                raise MediaExtractionError(
                    f"Video observed duration exceeds the {max_video_seconds:g}s limit"
                )
            if timestamp + 1e-9 < config.start_time:
                continue
            if config.end_time is not None and timestamp >= config.end_time - 1e-9:
                break

            last_frame = frame.copy()
            last_frame_number = frame_number - 1
            last_frame_timestamp = timestamp
            scene = cv2_module.resize(frame, (64, 36), interpolation=cv2_module.INTER_AREA)
            scene_delta = 0.0
            if previous_scene is not None and scene_threshold > 0:
                scene_delta = float(
                    np.mean(np.abs(scene.astype(np.float32) - previous_scene.astype(np.float32))) / 255.0
                )
            previous_scene = scene

            first_candidate = not result and include_first
            interval_candidate = timestamp + 1e-9 >= next_timestamp
            scene_candidate = scene_threshold > 0 and scene_delta >= scene_threshold
            if not (first_candidate or interval_candidate or scene_candidate):
                continue
            output = destination / f"frame-{len(result):06d}.jpg"
            width, height = _save_frame(cv2_module, frame, output)
            captured = CapturedFrame(
                index=frame_number - 1,
                timestamp=round(float(timestamp), 6),
                path=output,
                width=width,
                height=height,
            )
            result.append(captured)
            if on_frame:
                on_frame(captured)
            if len(result) >= config.max_frames:
                break
            next_timestamp = (
                config.start_time
                + (math.floor((timestamp - config.start_time + 1e-9) / config.interval) + 1) * config.interval
            )

        if include_last and last_frame is not None and len(result) < config.max_frames:
            if not result or last_frame_timestamp > result[-1].timestamp + 1e-6:
                output = destination / f"frame-{len(result):06d}.jpg"
                width, height = _save_frame(cv2_module, last_frame, output)
                captured = CapturedFrame(
                    index=last_frame_number,
                    timestamp=round(float(last_frame_timestamp), 6),
                    path=output,
                    width=width,
                    height=height,
                )
                result.append(captured)
                if on_frame:
                    on_frame(captured)
    finally:
        capture.release()

    if not result:
        raise MediaExtractionError(f"No frames were available in {source} for the requested time range.")
    return result


def thumbnail_path(frame_path: Path | str) -> Path:
    path = Path(frame_path)
    return path.with_name(f"{path.stem}-thumb.jpg")


def create_thumbnail(frame_path: Path | str, max_dimension: int = MAX_THUMBNAIL_DIMENSION) -> Path:
    """Create a small JPEG beside a full frame using the stable file convention."""

    source = Path(frame_path)
    destination = thumbnail_path(source)
    max_dimension = max(1, min(int(max_dimension), MAX_THUMBNAIL_DIMENSION))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with Image.open(source) as image:
            thumbnail = image.convert("RGB")
            thumbnail.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
            thumbnail.save(destination, format="JPEG", quality=85, optimize=True)
    except Exception as exc:
        raise MediaExtractionError(f"Could not create thumbnail for {source}: {exc}") from exc
    return destination


def probe_media_duration(
    source_path: Path | str,
    *,
    cancel_event: Event | None = None,
    timeout: float = 30,
    deadline: float | None = None,
) -> float | None:
    """Best-effort bounded ffprobe duration, useful for hard duration guards."""

    executable = shutil.which("ffprobe")
    if not executable:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    try:
        result = run_process(
            command,
            cancel_event=cancel_event,
            timeout=_remaining_timeout(deadline, min(float(timeout), 30.0)),
        )
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration >= 0 else None
    except CancelledProcessing:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def probe_media_dimensions(
    source_path: Path | str,
    *,
    cancel_event: Event | None = None,
    timeout: float = 30,
    deadline: float | None = None,
) -> tuple[int, int] | None:
    """Return the first video stream dimensions through a bounded ffprobe."""

    executable = shutil.which("ffprobe")
    if not executable:
        return None
    command = [
        executable,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(source_path),
    ]
    try:
        result = run_process(
            command,
            cancel_event=cancel_event,
            timeout=_remaining_timeout(deadline, min(float(timeout), 30.0)),
        )
        width_text, height_text = result.stdout.strip().split("x", 1)
        width, height = int(width_text), int(height_text)
        if width <= 0 or height <= 0:
            return None
        return width, height
    except CancelledProcessing:
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
