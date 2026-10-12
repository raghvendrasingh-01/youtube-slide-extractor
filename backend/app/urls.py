"""Strict source normalisation and bounded YouTube playlist discovery.

The backend deliberately has one network input: canonical HTTPS YouTube
links.  Everything else is a local path which is authorised by the processing
pipeline before it is opened.  Keeping this boundary here means yt-dlp never
gets an attacker-selected generic URL or an authentication hint.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from .errors import CancelledProcessing, SourceAccessError, SourceParseError
from .processes import process_error, run_process


YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
        "youtu.be",
    }
)
CANONICAL_YOUTUBE_HOST = "www.youtube.com"
# YouTube video IDs are exactly eleven URL-safe characters.  Do not widen
# this expression for fixtures: accepting arbitrary IDs can turn an otherwise
# safe input into an extractor redirect.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,200}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_YOUTUBE_PREFIX_RE = re.compile(
    r"^(?:(?:www\.|m\.|music\.)?(?:youtube\.com|youtube-nocookie\.com)|youtu\.be)(?:/|$)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedSource:
    """A canonical input source before optional playlist expansion.

    Metadata is deliberately optional because flat playlist extraction can
    return a partially inaccessible entry.  Such entries remain in the list
    with ``error`` populated so the job can report them without stopping the
    rest of the playlist.
    """

    original: str
    canonical: str
    kind: str
    title: str = ""
    video_id: str | None = None
    playlist_id: str | None = None
    duration: float | None = None
    channel: str | None = None
    thumbnail: str | None = None
    playlist_count: int | None = None
    position: int = 0
    error: str | None = None


def _youtube_host(host: str | None) -> str:
    return (host or "").lower()


def _valid_id(value: str | None) -> str | None:
    if value and _VIDEO_ID_RE.fullmatch(value):
        return value
    return None


def _valid_playlist_id(value: str | None) -> str | None:
    if value and _PLAYLIST_ID_RE.fullmatch(value):
        return value
    return None


def _ensure_no_controls(value: str, *, what: str = "source") -> None:
    if _CONTROL_RE.search(value):
        raise SourceParseError(f"{what} contains a control character")


def _url_metadata(entry: dict[str, Any], key: str) -> Any:
    value = entry.get(key)
    if value is None and key == "channel":
        value = entry.get("uploader") or entry.get("channel_name")
    if value is None and key == "thumbnail":
        thumbnails = entry.get("thumbnails")
        value = entry.get("thumbnail")
        if value is None and isinstance(thumbnails, list) and thumbnails:
            last = thumbnails[-1]
            if isinstance(last, dict):
                value = last.get("url")
    return value


def _parse_youtube(url: str, original: str) -> ParsedSource:
    parsed = urlparse(url)
    host = _youtube_host(parsed.hostname)
    if parsed.scheme.lower() != "https" or host not in YOUTUBE_HOSTS:
        raise SourceParseError("only canonical HTTPS YouTube URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise SourceParseError("URLs with embedded credentials are not supported")
    # Accessing .port also validates malformed port syntax.  Explicit ports,
    # including :443, are rejected rather than normalised away.
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise SourceParseError(f"YouTube URL has an invalid port: {original!r}") from exc
    if explicit_port is not None or parsed.netloc.endswith(":"):
        raise SourceParseError("YouTube URLs must not include a port")
    if parsed.params:
        raise SourceParseError("YouTube URLs must not include path parameters")

    path_parts = [unquote(part) for part in parsed.path.split("/") if part]
    for part in path_parts:
        _ensure_no_controls(part, what="YouTube URL path")
    _ensure_no_controls(unquote(parsed.fragment), what="YouTube URL fragment")
    query = parse_qs(parsed.query, keep_blank_values=True)
    for values in query.values():
        for value in values:
            _ensure_no_controls(value, what="YouTube URL query")
    raw_playlist = (query.get("list") or [""])[0]
    if len(query.get("v") or []) > 1 or len(query.get("list") or []) > 1:
        raise SourceParseError("YouTube URLs must contain one video and playlist identifier at most")
    playlist_id = _valid_playlist_id(raw_playlist)
    if raw_playlist and playlist_id is None:
        raise SourceParseError("YouTube playlist IDs contain invalid characters")

    video_id: str | None = None
    if host == "youtu.be":
        video_id = _valid_id(path_parts[0] if len(path_parts) == 1 else None)
    elif path_parts and path_parts[0].lower() == "watch":
        if len(path_parts) != 1:
            raise SourceParseError(f"Unsupported YouTube path: {original!r}")
        video_id = _valid_id((query.get("v") or [None])[0])
    elif path_parts and path_parts[0].lower() in {"shorts", "embed", "live", "v"}:
        video_id = _valid_id(path_parts[1] if len(path_parts) == 2 else None)
    elif path_parts and path_parts[0].lower() == "playlist":
        if len(path_parts) != 1:
            raise SourceParseError(f"Unsupported YouTube playlist path: {original!r}")
    elif path_parts and path_parts[0].lower() == "attribution_link":
        # Shared attribution links contain an escaped relative watch URL.  It
        # is accepted only when the nested target is itself a strict YouTube
        # path; it never becomes a generic redirect target.
        nested = (query.get("u") or [""])[0]
        if nested.startswith("/"):
            nested_parsed = urlparse("https://" + CANONICAL_YOUTUBE_HOST + nested)
            nested_query = parse_qs(nested_parsed.query, keep_blank_values=True)
            video_id = _valid_id((nested_query.get("v") or [None])[0])
            if playlist_id is None:
                playlist_id = _valid_playlist_id((nested_query.get("list") or [None])[0])

    if video_id:
        return ParsedSource(
            original=original,
            canonical=f"https://{CANONICAL_YOUTUBE_HOST}/watch?v={quote(video_id, safe='-_')}",
            kind="youtube_video",
            title=video_id,
            video_id=video_id,
            playlist_id=playlist_id,
        )
    if playlist_id and path_parts and path_parts[0].lower() in {"playlist", "watch"}:
        return ParsedSource(
            original=original,
            canonical=f"https://{CANONICAL_YOUTUBE_HOST}/playlist?{urlencode({'list': playlist_id})}",
            kind="youtube_playlist",
            title=f"Playlist {playlist_id}",
            playlist_id=playlist_id,
        )
    raise SourceParseError(
        f"Unsupported or incomplete YouTube URL: {original!r}. "
        "Expected an HTTPS watch, youtu.be, shorts, embed, live, or playlist link."
    )


def parse_source(value: str) -> ParsedSource:
    """Parse one local path or a strictly allow-listed YouTube URL."""

    if not isinstance(value, str):
        raise SourceParseError("source must be a string")
    _ensure_no_controls(value)
    original = value.strip()
    if not original:
        raise SourceParseError("source cannot be empty")

    if original.lower().startswith("file://"):
        file_url = urlparse(original)
        if file_url.scheme.lower() != "file" or file_url.username is not None or file_url.password is not None:
            raise SourceParseError("file:// sources cannot contain credentials")
        try:
            file_port = file_url.port
        except ValueError as exc:
            raise SourceParseError("file:// sources cannot contain a port") from exc
        if file_port is not None or file_url.netloc not in {"", "localhost"} or file_url.query or file_url.fragment:
            raise SourceParseError("file:// sources must refer to a local file without host, port, query, or fragment")
        if not file_url.path:
            raise SourceParseError("file:// source path cannot be empty")
        path = Path(unquote(file_url.path)).expanduser()
        _ensure_no_controls(str(path), what="file:// source path")
        canonical = str(path.resolve(strict=False))
        return ParsedSource(original, canonical, "local", path.stem or path.name)

    candidate = original
    # A bare YouTube hostname is a convenience input; it is always upgraded
    # to the canonical HTTPS form before validation.  Protocol-relative
    # arbitrary URLs are not accepted.
    if _YOUTUBE_PREFIX_RE.match(candidate):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname
        # Validate malformed IPv6/ports even though generic hosts are rejected.
        _ = parsed.port
    except ValueError as exc:
        raise SourceParseError(f"Malformed source URL: {original!r}") from exc

    if parsed.scheme:
        if parsed.scheme.lower() != "https":
            if parsed.scheme.lower() in {"http", "https"}:
                raise SourceParseError("network sources must use HTTPS YouTube URLs")
            raise SourceParseError(
                f"Unsupported source scheme {parsed.scheme!r}; use a local file or HTTPS YouTube link."
            )
        if parsed.username is not None or parsed.password is not None:
            raise SourceParseError("URLs with embedded credentials are not supported")
        try:
            explicit_port = parsed.port
        except ValueError as exc:
            raise SourceParseError(f"Malformed source URL: {original!r}") from exc
        if explicit_port is not None or parsed.netloc.endswith(":"):
            raise SourceParseError("network URLs must not include a port")
        if not host or _youtube_host(host) not in YOUTUBE_HOSTS:
            raise SourceParseError("only allow-listed HTTPS YouTube URLs are supported")
        return _parse_youtube(candidate, original)

    if candidate.startswith("//"):
        raise SourceParseError("protocol-relative network URLs are not supported")
    path = Path(original).expanduser()
    canonical = str(path.resolve(strict=False))
    return ParsedSource(original, canonical, "local", path.stem or path.name)


def _source_key(source: ParsedSource) -> tuple[str, str]:
    if source.video_id:
        return ("video_id", source.video_id)
    return ("canonical", source.canonical)


def parse_sources(values: str | Iterable[str]) -> list[ParsedSource]:
    """Parse multi-line input and remove semantic duplicates in order."""

    if isinstance(values, str):
        candidates = values.splitlines()
    else:
        candidates = []
        iterable = sorted(values, key=str) if isinstance(values, set) else values
        for value in iterable:
            candidates.extend(str(value).splitlines())
    result: list[ParsedSource] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        text = candidate.strip()
        if text.startswith("- "):
            text = text[2:].strip()
        if not text:
            continue
        parsed = parse_source(text)
        key = _source_key(parsed)
        if key in seen:
            continue
        seen.add(key)
        result.append(parsed)
    if not result:
        raise SourceParseError("at least one non-empty source is required")
    return result


def _entry_error(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return "Playlist entry was unavailable"
    availability = str(entry.get("availability") or "").lower()
    if availability in {
        "private",
        "premium_only",
        "subscriber_only",
        "needs_auth",
        "unavailable",
        "deleted",
        "login_required",
    }:
        return f"YouTube entry is unavailable ({availability.replace('_', ' ')})"
    error = entry.get("error")
    if error:
        return str(error)
    title = str(entry.get("title") or "").lower()
    if any(marker in title for marker in ("private video", "deleted video", "removed video")):
        return "YouTube entry is unavailable"
    if entry.get("is_live") or entry.get("live") or entry.get("live_status") in {"is_live", "is_upcoming"}:
        return "Live streams are not supported"
    return None


def _playlist_entry(
    source: ParsedSource,
    entry: dict[str, Any] | None,
    position: int,
    playlist_count: int,
) -> ParsedSource:
    entry = entry or {}
    entry_id = _valid_id(str(entry.get("id") or ""))
    webpage = entry.get("webpage_url") or entry.get("original_url") or entry.get("url")
    if entry_id is None and webpage:
        entry_id = _valid_id(str(webpage))
    if entry_id is None and webpage:
        try:
            parsed = parse_source(str(webpage))
        except SourceParseError:
            parsed = None
        if parsed is not None:
            entry_id = parsed.video_id
    title = str(entry.get("title") or (entry_id or f"Playlist entry {position}"))
    error = _entry_error(entry)
    if entry_id:
        canonical = f"https://{CANONICAL_YOUTUBE_HOST}/watch?v={quote(entry_id, safe='-_')}"
        return ParsedSource(
            original=str(webpage or canonical),
            canonical=canonical,
            kind="youtube_video",
            title=title,
            video_id=entry_id,
            playlist_id=source.playlist_id,
            duration=_finite_optional(entry.get("duration")),
            channel=str(_url_metadata(entry, "channel") or "") or None,
            thumbnail=str(_url_metadata(entry, "thumbnail") or "") or None,
            playlist_count=playlist_count,
            position=position,
            error=error,
        )

    # Keep an inaccessible/malformed entry visible.  It is intentionally not
    # handed back to yt-dlp for download; the processing layer reports its
    # error and continues with later entries.
    return ParsedSource(
        original=str(webpage or source.original),
        canonical=f"{source.canonical}#entry-{position}",
        kind="youtube_video",
        title=title,
        playlist_id=source.playlist_id,
        playlist_count=playlist_count,
        position=position,
        error=error or "Playlist entry did not contain a valid 11-character video ID",
    )


def _finite_optional(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 and number != float("inf") and number == number else None


def discover_playlist(
    source: ParsedSource,
    yt_dlp_module: Any = None,
    *,
    cancel_event: Event | None = None,
    timeout: int = 120,
    max_items: int = 30,
) -> list[ParsedSource]:
    """Expand a YouTube playlist in extractor order without downloading it."""

    if source.kind != "youtube_playlist":
        return [source]
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("Job cancelled before playlist discovery")
    max_items = max(1, min(int(max_items), 2000))
    options = {
        "quiet": True,
        "no_warnings": True,
        "ignoreconfig": True,
        "noplaylist": False,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "playlistend": max_items,
        "socket_timeout": min(max(1, timeout), 15),
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "allowed_extractors": ["youtube"],
        "cachedir": False,
    }
    try:
        if yt_dlp_module is not None and cancel_event is None:
            with yt_dlp_module.YoutubeDL(options) as ydl:
                info = ydl.extract_info(source.canonical, download=False)
        else:
            result = run_process(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-cookies",
                    "--flat-playlist",
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
                    "--playlist-end",
                    str(max_items),
                    "--",
                    source.canonical,
                ],
                cancel_event=cancel_event,
                timeout=min(max(1, timeout), 120),
            )
            info = json.loads(result.stdout)
    except CancelledProcessing:
        raise
    except Exception as exc:  # yt-dlp exposes several exception types
        raise SourceAccessError(
            f"Could not access YouTube playlist {source.canonical}: {process_error(exc)}"
        ) from exc

    raw_entries = (info or {}).get("entries") or []
    playlist_count = _finite_optional((info or {}).get("playlist_count"))
    count = int(playlist_count) if playlist_count is not None else int(_finite_optional((info or {}).get("n_entries")) or 0)
    entries: list[Any] = []
    for entry in raw_entries:
        if len(entries) >= max_items:
            break
        entries.append(entry)
    if count <= 0:
        count = len(entries)
    discovered: list[ParsedSource] = []
    seen_ids: set[str] = set()
    for position, entry in enumerate(entries, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledProcessing("Job cancelled during playlist discovery")
        parsed = _playlist_entry(source, entry if isinstance(entry, dict) else None, position, count)
        if parsed.video_id and parsed.video_id in seen_ids:
            continue
        if parsed.video_id:
            seen_ids.add(parsed.video_id)
        discovered.append(parsed)
    if not discovered:
        return [
            replace(
                source,
                playlist_count=count,
                position=1,
                error="YouTube playlist was accessible but contained no entries",
            )
        ]
    return discovered


def expand_sources(
    values: str | Iterable[str],
    yt_dlp_module: Any = None,
    *,
    cancel_event: Event | None = None,
    timeout: int = 120,
    max_items: int = 30,
) -> list[ParsedSource]:
    """Parse and expand playlists while preserving order and video-ID identity."""

    expanded: list[ParsedSource] = []
    seen: set[tuple[str, str]] = set()
    for source in parse_sources(values):
        try:
            items = discover_playlist(
                source,
                yt_dlp_module=yt_dlp_module,
                cancel_event=cancel_event,
                timeout=timeout,
                max_items=max_items,
            )
        except CancelledProcessing:
            raise
        except Exception as exc:
            # Preserve a failed playlist as a reportable source, while still
            # allowing later independent inputs to run.
            items = [replace(source, position=1, error=process_error(exc))]
        for item in items:
            key = _source_key(item)
            if key in seen:
                continue
            if item.position == 0:
                item = replace(item, position=len(expanded) + 1)
            expanded.append(item)
            seen.add(key)
    if not expanded:
        raise SourceParseError("at least one source is required")
    return expanded


def analyze_sources(values: str | Iterable[str], settings: Any, cancel_event: Event | None = None) -> list[dict[str, Any]]:
    """Compatibility entrypoint for callers that keep source helpers in ``urls``.

    The implementation lives in ``processing`` because it also performs local
    authorization and bounded media probing.  Importing lazily avoids the
    normal ``processing -> urls`` module cycle.
    """

    from .processing import analyze_sources as _analyze_sources

    return _analyze_sources(values, settings, cancel_event=cancel_event)
