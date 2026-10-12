"""Synchronous, bounded job pipeline executed by the background worker thread."""

from __future__ import annotations

import shutil
import time
from datetime import timedelta
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterable

from .config import AppSettings
from .dedupe import FrameDeduplicator
from .errors import CancelledProcessing, MediaExtractionError, SourceAccessError, SourceParseError
from .media import (
    ExtractionConfig,
    IMAGE_EXTENSIONS,
    _validate_remote_metadata,
    check_local_authorized,
    create_thumbnail,
    download_remote_source,
    extract_frames,
    extract_remote_metadata,
    probe_media_duration,
)
from .models import FrameMetadata, JobRecord, SourceRecord
from .ocr import create_ocr
from .store import JobStore
from .urls import ParsedSource, discover_playlist, expand_sources, parse_source


FRAME_PERSIST_BATCH = 8


def _check_cancel(cancel_event: Event, deadline: float | None = None) -> None:
    if cancel_event.is_set():
        raise CancelledProcessing("job cancelled by user")
    if deadline is not None and time.monotonic() >= deadline:
        raise MediaExtractionError("job exceeded its maximum wall-clock time")


def _source_id(index: int) -> str:
    return f"source-{index:04d}"


def _source_records(sources: list[ParsedSource]) -> list[SourceRecord]:
    records: list[SourceRecord] = []
    for index, source in enumerate(sources, start=1):
        records.append(
            SourceRecord(
                id=_source_id(index),
                input=source.original,
                canonical=source.canonical,
                kind=source.kind,  # type: ignore[arg-type]
                title=source.title or source.video_id or source.canonical,
                status="failed" if source.error else "queued",
                error=source.error,
                video_id=source.video_id,
                duration=source.duration,
                channel=source.channel,
                thumbnail=source.thumbnail,
                playlist_count=source.playlist_count,
                position=source.position or index,
            )
        )
    return records


def _source_progress(
    position: int,
    total_sources: int,
    *,
    timestamp: float | None = None,
    duration: float | None = None,
    frame_count: int = 0,
    frame_cap: int = 1,
) -> float:
    """Map observed timestamp/frame work to the current source's progress span."""

    total_sources = max(1, total_sources)
    base = 5.0 + ((position - 1) / total_sources) * 90.0
    span = 90.0 / total_sources
    if duration is not None and duration > 0 and timestamp is not None:
        fraction = timestamp / duration
    else:
        fraction = frame_count / max(1, frame_cap)
    fraction = min(0.94, max(0.0, fraction))
    return round(base + span * fraction, 2)


def _job_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def _ensure_job_bytes(store: JobStore, job_id: str, maximum: int) -> None:
    size = _job_bytes(store.job_dir(job_id))
    if size > maximum:
        raise MediaExtractionError(f"job data exceeds the {maximum} byte limit")


def _new_deduplicator(request, root_dir: Path, cancel_event: Event) -> FrameDeduplicator:
    """Use the new configurable deduplicator while retaining old embedders."""

    try:
        return FrameDeduplicator(
            mode=request.dedupe_mode,
            root_dir=root_dir,
            cancel_event=cancel_event,
            phash_distance=request.phash_distance,
        )
    except TypeError:
        # Older installed visual-agent modules did not expose phash_distance;
        # their public mode/root/cancellation contract remains compatible.
        return FrameDeduplicator(
            mode=request.dedupe_mode,
            root_dir=root_dir,
            cancel_event=cancel_event,
        )


def _set_source_status(record: JobRecord, source_id: str, status: str) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.status = status  # type: ignore[assignment]
            return


def _fail_source(record: JobRecord, source_id: str, error: str, count: int | None = None) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.status = "failed"
            source.error = error
            if count is not None:
                source.frame_count = max(source.frame_count, count)
            return


def _cancel_source(record: JobRecord, source_id: str, count: int) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.status = "cancelled"
            source.frame_count = max(source.frame_count, count)
            return


def _set_source_title(record: JobRecord, source_id: str, title: str) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.title = title
            for frame in record.frames:
                if frame.source_id == source_id:
                    frame.source_title = title
            return


def _set_source_metadata(
    record: JobRecord,
    source_id: str,
    *,
    title: str | None = None,
    duration: float | None = None,
    channel: str | None = None,
    thumbnail: str | None = None,
) -> None:
    for source in record.sources:
        if source.id == source_id:
            if title:
                source.title = title
            if duration is not None:
                source.duration = duration
            if channel:
                source.channel = channel
            if thumbnail:
                source.thumbnail = thumbnail
            for frame in record.frames:
                if frame.source_id == source_id and title:
                    frame.source_title = title
            return


def _complete_source(
    record: JobRecord,
    source_id: str,
    count: int,
    *,
    retained: int = 0,
    exact_removed: int = 0,
    near_removed: int = 0,
    processing_seconds: float = 0,
) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.status = "completed"
            source.frame_count = count
            source.retained_count = retained
            source.exact_removed = exact_removed
            source.near_removed = near_removed
            source.processing_seconds = processing_seconds
            return


def _source_metrics(frames: list[FrameMetadata]) -> tuple[int, int, int]:
    retained = sum(1 for frame in frames if frame.retained)
    exact = sum(1 for frame in frames if frame.duplicate_reason == "exact_hash")
    near = sum(1 for frame in frames if frame.duplicate_of and frame.duplicate_reason != "exact_hash")
    return retained, exact, near


def _frame_analysis_record(source: ParsedSource, *, error: str | None = None) -> dict[str, Any]:
    return {
        "input": source.original,
        "canonical": source.canonical,
        "kind": source.kind,
        "title": source.title,
        "video_id": source.video_id,
        "duration": source.duration,
        "channel": source.channel,
        "thumbnail": source.thumbnail,
        "playlist_count": source.playlist_count,
        "error": error if error is not None else source.error,
    }


def _analysis_lines(values: str | Iterable[str]) -> list[str]:
    if isinstance(values, str):
        raw = values.splitlines()
    else:
        raw = []
        for value in values:
            raw.extend(str(value).splitlines())
    lines: list[str] = []
    for value in raw:
        cleaned = value.strip()
        if cleaned.startswith("- "):
            cleaned = cleaned[2:].strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def analyze_sources(
    values: str | Iterable[str],
    settings: AppSettings,
    cancel_event: Event | None = None,
) -> list[dict[str, Any]]:
    """Return bounded real metadata without downloading media.

    Each input and each playlist entry is independent: a private/deleted item
    is returned with an error while later inputs continue.  Local files are
    authorized against configured roots only; a job work root is deliberately
    unavailable to this preflight helper because it has no job identity.
    """

    cancel_event = cancel_event or Event()
    deadline = time.monotonic() + min(settings.source_download_timeout, settings.max_job_seconds)
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for line in _analysis_lines(values):
        _check_cancel(cancel_event, deadline)
        timeout = max(0.1, deadline - time.monotonic())
        try:
            source = parse_source(line)
        except SourceParseError as exc:
            results.append(
                {
                    "input": line,
                    "canonical": "",
                    "kind": "invalid",
                    "title": line,
                    "video_id": None,
                    "duration": None,
                    "channel": None,
                    "thumbnail": None,
                    "playlist_count": None,
                    "error": str(exc),
                }
            )
            continue

        try:
            items = discover_playlist(
                source,
                cancel_event=cancel_event,
                timeout=timeout,
                max_items=settings.playlist_max_items,
            ) if source.kind == "youtube_playlist" else [source]
        except CancelledProcessing:
            raise
        except Exception as exc:
            results.append(_frame_analysis_record(source, error=str(exc) or exc.__class__.__name__))
            continue

        for item in items:
            _check_cancel(cancel_event, deadline)
            item_timeout = max(0.1, deadline - time.monotonic())
            key = ("video_id", item.video_id) if item.video_id else ("canonical", item.canonical)
            if key in seen:
                continue
            seen.add(key)
            record = _frame_analysis_record(item)
            if item.error:
                results.append(record)
                continue
            try:
                if item.kind == "local":
                    local = check_local_authorized(
                        item.canonical,
                        settings.normalized_authorized_roots(),
                        max_bytes=settings.max_source_bytes,
                    )
                    duration = 0.0 if local.suffix.lower() in IMAGE_EXTENSIONS else probe_media_duration(
                        local, cancel_event=cancel_event, timeout=min(item_timeout, 30)
                    )
                    if duration is None:
                        raise SourceAccessError("Local video duration could not be determined by ffprobe")
                    if duration is not None and duration > settings.max_video_seconds:
                        raise SourceAccessError(
                            f"Local source duration {duration:g}s exceeds the {settings.max_video_seconds:g}s limit"
                        )
                    record["title"] = local.stem or local.name
                    record["duration"] = duration
                else:
                    info = extract_remote_metadata(
                        item,
                        cancel_event=cancel_event,
                        timeout=item_timeout,
                    )
                    duration, _ = _validate_remote_metadata(
                        info,
                        max_source_bytes=settings.max_source_bytes,
                        max_video_seconds=settings.max_video_seconds,
                    )
                    record["title"] = str(info.get("title") or item.title)
                    record["duration"] = duration
                    record["channel"] = str(info.get("channel") or info.get("uploader") or "") or None
                    record["thumbnail"] = str(info.get("thumbnail") or "") or None
            except CancelledProcessing:
                raise
            except Exception as exc:
                record["error"] = str(exc) or exc.__class__.__name__
            results.append(record)
    if not results:
        raise SourceParseError("at least one non-empty source is required")
    return results


def process_job(
    job_id: str,
    store: JobStore,
    settings: AppSettings,
    cancel_event: Event,
    progress_callback: Callable[[JobRecord], None] | None = None,
) -> JobRecord:
    """Process all sources incrementally and persist meaningful batches."""

    started = time.monotonic()
    deadline = started + settings.max_job_seconds
    initial = store.get(job_id)
    request = initial.request
    all_frames: list[FrameMetadata] = list(initial.frames)
    records: list[SourceRecord] = list(initial.sources)
    active_source: SourceRecord | None = None
    partial_frames: list[FrameMetadata] = []
    store.update(job_id, status="running", stage="parsing sources", progress=1.0, error=None)
    try:
        _check_cancel(cancel_event)
        _check_cancel(cancel_event, deadline)
        sources = expand_sources(
            request.sources,
            cancel_event=cancel_event,
            timeout=min(settings.source_download_timeout, settings.max_job_seconds),
            max_items=settings.playlist_max_items,
        )
        records = _source_records(sources)
        store.update(
            job_id,
            sources=records,
            stage="resolving sources",
            progress=3.0,
            ocr_status="requested" if request.ocr_enabled else "disabled",
        )
        if progress_callback:
            progress_callback(store.get(job_id))

        extracted_total = 0
        ocr = create_ocr(
            request.ocr_enabled,
            timeout=min(20, settings.max_job_seconds),
            cancel_event=cancel_event,
        )
        store.update(job_id, ocr_status=("enabled" if ocr.enabled else ocr.reason or "disabled"))
        # Keep one ordered representative index for the combined document. A
        # repeated intro/title slide in a later playlist item is therefore
        # reported as a duplicate instead of silently bloating the combined
        # export. The source id is still retained on every frame for review.
        deduplicator = _new_deduplicator(request, store.job_dir(job_id), cancel_event)

        for position, source in enumerate(sources, start=1):
            _check_cancel(cancel_event, deadline)
            source_record = records[position - 1]
            active_source = source_record
            partial_frames = []
            remaining_total = settings.max_total_frames - extracted_total
            if remaining_total <= 0:
                for skipped in records[position - 1 :]:
                    skipped.status = "skipped"
                    skipped.error = "Job total frame cap reached"
                warning = f"Skipped sources after reaching the {settings.max_total_frames} frame job cap"
                store.mutate(job_id, lambda record, warning=warning: _append_warning(record, warning))
                store.update(job_id, sources=records, stage="job frame cap reached", progress=95.0)
                break
            if source.error:
                source_record.status = "failed"
                source_record.error = source.error
                warning = f"{source_record.title}: {source.error}"
                store.mutate(
                    job_id,
                    lambda record, source_id=source_record.id, error=source.error, warning=warning: (
                        _fail_source(record, source_id, error),
                        _append_warning(record, warning),
                    ),
                )
                continue

            per_video_limit = min(request.max_frames, remaining_total)
            source_started = time.monotonic()
            try:
                _process_source(
                    job_id=job_id,
                    source=source,
                    source_record=source_record,
                    position=position,
                    total_sources=len(sources),
                    remaining=per_video_limit,
                    request=request,
                    store=store,
                    settings=settings,
                    cancel_event=cancel_event,
                    ocr=ocr,
                    all_frames=all_frames,
                    partial_frames=partial_frames,
                    deadline=deadline,
                    progress_callback=progress_callback,
                    deduplicator=deduplicator,
                )
                extracted_total += len(partial_frames)
                all_frames.extend(partial_frames)
                source_record.status = "completed"
                source_record.frame_count = len(partial_frames)
                source_record.processing_seconds = round(time.monotonic() - source_started, 3)
                retained, exact, near = _source_metrics(partial_frames)
                source_record.retained_count = retained
                source_record.exact_removed = exact
                source_record.near_removed = near
                store.mutate(
                    job_id,
                    lambda record, source_id=source_record.id, count=len(partial_frames), retained=retained, exact=exact, near=near, seconds=source_record.processing_seconds: _complete_source(
                        record,
                        source_id,
                        count,
                        retained=retained,
                        exact_removed=exact,
                        near_removed=near,
                        processing_seconds=seconds,
                    ),
                )
                store.update(
                    job_id,
                    frames=all_frames,
                    sources=records,
                    stage=f"completed source {position} of {len(sources)}",
                    progress=_source_progress(
                        position,
                        len(sources),
                        timestamp=source_record.duration,
                        duration=source_record.duration,
                        frame_count=len(partial_frames),
                        frame_cap=per_video_limit,
                    ),
                )
                if progress_callback:
                    progress_callback(store.get(job_id))
            except CancelledProcessing:
                raise
            except Exception as exc:
                extracted_total += len(partial_frames)
                all_frames.extend(partial_frames)
                message = str(exc) or exc.__class__.__name__
                source_record.status = "failed"
                source_record.error = message
                source_record.frame_count = len(partial_frames)
                source_record.processing_seconds = round(time.monotonic() - source_started, 3)
                # A job deadline is fatal; continuing would only turn one
                # bounded failure into a series of misleading source errors.
                if time.monotonic() >= deadline:
                    raise
                warning = f"{source_record.title}: {message}"
                store.mutate(
                    job_id,
                    lambda record, source_id=source_record.id, error=message, count=len(partial_frames), warning=warning, seconds=source_record.processing_seconds: (
                        _fail_source(record, source_id, error, count),
                        _append_warning(record, warning),
                        _set_source_processing_seconds(record, source_id, seconds),
                    ),
                )
                store.update(
                    job_id,
                    frames=all_frames,
                    sources=records,
                    stage=f"source {position} unavailable; continuing",
                    progress=round(5.0 + (position / len(sources)) * 90.0, 2),
                )
                if progress_callback:
                    progress_callback(store.get(job_id))
                continue

        successful = [source for source in records if source.status == "completed"]
        failed = [source for source in records if source.status == "failed"]
        warnings = list(store.get(job_id).warnings)
        if failed and successful:
            partial_warning = f"{len(failed)} source(s) failed; completed sources are available"
            if partial_warning not in warnings:
                warnings.append(partial_warning)
        if not successful:
            message = "All sources failed"
            if not failed:
                message = "No source produced frames"
            final = store.update(
                job_id,
                status="failed",
                stage="failed",
                progress=100.0,
                error=message,
                frames=all_frames,
                sources=records,
                warnings=warnings,
                processing_seconds=round(time.monotonic() - started, 3),
                expires_at=initial.created_at + timedelta(hours=settings.retention_hours),
            )
        else:
            final = store.update(
                job_id,
                status="completed",
                stage="completed",
                progress=100.0,
                error=None,
                frames=all_frames,
                sources=records,
                warnings=warnings,
                processing_seconds=round(time.monotonic() - started, 3),
                expires_at=initial.created_at + timedelta(hours=settings.retention_hours),
            )
        if progress_callback:
            progress_callback(final)
        return final
    except CancelledProcessing:
        # Keep any frames already committed.  A delete may race this write;
        # the store/worker boundary intentionally lets JobNotFound propagate so
        # no deleted job directory is recreated.
        all_frames.extend(partial_frames)
        cancelled = store.update(
            job_id,
            status="cancelled",
            stage="cancelled",
            error="Job cancelled by user",
            frames=all_frames,
            processing_seconds=round(time.monotonic() - started, 3),
            expires_at=initial.created_at + timedelta(hours=settings.retention_hours),
        )
        if active_source is not None:
            store.mutate(
                job_id,
                lambda record, source_id=active_source.id, count=len(partial_frames): _cancel_source(
                    record, source_id, count
                ),
            )
        if progress_callback:
            progress_callback(cancelled)
        return cancelled
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        all_frames.extend(partial_frames)
        current_warnings = store.get(job_id).warnings
        if active_source is not None:
            store.mutate(
                job_id,
                lambda record, source_id=active_source.id, error=message, count=len(partial_frames): _fail_source(
                    record, source_id, error, count
                ),
            )
        failed = store.update(
            job_id,
            status="failed",
            stage="failed",
            error=message,
            frames=all_frames,
            warnings=current_warnings,
            processing_seconds=round(time.monotonic() - started, 3),
            expires_at=initial.created_at + timedelta(hours=settings.retention_hours),
        )
        if progress_callback:
            progress_callback(failed)
        return failed
    finally:
        if store.exists(job_id):
            store.cleanup_work(job_id)


def _append_warning(record: JobRecord, warning: str) -> None:
    if warning and warning not in record.warnings:
        record.warnings.append(warning)


def _set_source_processing_seconds(record: JobRecord, source_id: str, seconds: float) -> None:
    for source in record.sources:
        if source.id == source_id:
            source.processing_seconds = seconds
            return


def _process_source(
    *,
    job_id: str,
    source: ParsedSource,
    source_record: SourceRecord,
    position: int,
    total_sources: int,
    remaining: int,
    request,
    store: JobStore,
    settings: AppSettings,
    cancel_event: Event,
    ocr,
    all_frames: list[FrameMetadata],
    partial_frames: list[FrameMetadata],
    deadline: float | None = None,
    progress_callback: Callable[[JobRecord], None] | None = None,
    deduplicator: FrameDeduplicator | None = None,
):
    """Prepare and process one source with one dedupe scope per video."""

    _check_cancel(cancel_event, deadline)
    source_record.status = "running"
    store.mutate(
        job_id,
        lambda record, source_id=source_record.id: _set_source_status(record, source_id, "running"),
    )
    source_work = store.work_dir(job_id) / source_record.id
    source_work.mkdir(parents=True, exist_ok=True)
    media_path: Path | None = None
    remote_download = source.kind != "local"
    store.update(
        job_id,
        stage=f"preparing source {position} of {total_sources}",
        progress=_source_progress(position, total_sources, frame_count=0, frame_cap=remaining),
    )
    try:
        if source.kind == "local":
            media_path = check_local_authorized(
                source.canonical,
                settings.normalized_authorized_roots(),
                job_work_root=store.work_dir(job_id),
                max_bytes=settings.max_source_bytes,
            )
        else:
            remote_info = extract_remote_metadata(
                source,
                cancel_event=cancel_event,
                timeout=min(120.0, max(0.1, (deadline - time.monotonic()) if deadline is not None else settings.source_download_timeout)),
            )
            remote_duration, remote_title = _validate_remote_metadata(
                remote_info,
                max_source_bytes=settings.max_source_bytes,
                max_video_seconds=settings.max_video_seconds,
            )
            source_record.duration = remote_duration
            source_record.channel = str(remote_info.get("channel") or remote_info.get("uploader") or "") or None
            source_record.thumbnail = str(remote_info.get("thumbnail") or "") or None
            if remote_title:
                source_record.title = remote_title
            store.mutate(
                job_id,
                lambda record, source_id=source_record.id, title=source_record.title, duration=remote_duration, channel=source_record.channel, thumbnail=source_record.thumbnail: _set_source_metadata(
                    record,
                    source_id,
                    title=title,
                    duration=duration,
                    channel=channel,
                    thumbnail=thumbnail,
                ),
            )
            media_path, discovered_title = download_remote_source(
                source,
                source_work / "download",
                cancel_event=cancel_event,
                timeout=max(1, int((deadline - time.monotonic()) if deadline is not None else settings.source_download_timeout)),
                max_source_bytes=settings.max_source_bytes,
                max_video_seconds=settings.max_video_seconds,
                deadline=deadline,
                metadata=remote_info,
            )
            if discovered_title:
                source_record.title = discovered_title
                store.mutate(
                    job_id,
                    lambda record, source_id=source_record.id, title=discovered_title: _set_source_title(
                        record, source_id, title
                    ),
                )

        _check_cancel(cancel_event, deadline)
        probed_duration = probe_media_duration(
            media_path,
            cancel_event=cancel_event,
            timeout=30.0,
            deadline=deadline,
        )
        if probed_duration is not None:
            if probed_duration > settings.max_video_seconds:
                raise MediaExtractionError(
                    f"Video duration {probed_duration:g}s exceeds the {settings.max_video_seconds:g}s limit"
                )
            source_record.duration = probed_duration

        active_deduplicator = deduplicator or _new_deduplicator(request, store.job_dir(job_id), cancel_event)

        def on_captured(captured) -> None:
            _check_cancel(cancel_event, deadline)
            frame_id = f"{source_record.id}-frame-{captured.index:08d}"
            frame = FrameMetadata(
                id=frame_id,
                source_id=source_record.id,
                source_title=source_record.title,
                index=captured.index,
                timestamp=captured.timestamp,
                path=captured.path.relative_to(store.job_dir(job_id)).as_posix(),
                width=captured.width,
                height=captured.height,
                image_url=f"/api/v1/jobs/{job_id}/frames/{frame_id}/image",
                thumbnail_url=f"/api/v1/jobs/{job_id}/frames/{frame_id}/thumbnail",
            )
            # OCR intentionally precedes comparison so the exact metadata that
            # is shown to a reviewer is also the metadata being persisted.
            if ocr.enabled:
                if deadline is not None and hasattr(ocr, "timeout"):
                    ocr.timeout = max(0.1, min(float(ocr.timeout), deadline - time.monotonic()))
                frame.ocr_text = ocr.extract(captured.path)
            compared = active_deduplicator.add(frame)
            partial_frames.append(compared)
            if len(partial_frames) % FRAME_PERSIST_BATCH == 0:
                progress = _source_progress(
                    position,
                    total_sources,
                    timestamp=captured.timestamp,
                    duration=source_record.duration,
                    frame_count=len(partial_frames),
                    frame_cap=remaining,
                )
                store.update(
                    job_id,
                    frames=all_frames + partial_frames,
                    stage=f"OCR/comparing source {position} of {total_sources} (frame {len(partial_frames)})",
                    progress=progress,
                )
                _ensure_job_bytes(store, job_id, settings.max_job_bytes)
                if progress_callback:
                    progress_callback(store.get(job_id))

        extraction = ExtractionConfig(
            interval=request.interval,
            start_time=request.start_time,
            end_time=request.end_time,
            max_frames=remaining,
        )
        try:
            extract_frames(
                media_path,
                store.frames_dir(job_id) / source_record.id,
                extraction,
                cancel_event=cancel_event,
                on_frame=on_captured,
                include_first=request.include_first,
                include_last=request.include_last,
                scene_threshold=request.scene_threshold,
                max_video_seconds=settings.max_video_seconds,
                deadline=deadline,
            )
        except Exception:
            # Captured frames have already gone through OCR/comparison.  Make
            # their thumbnails available even when a later decode fails.
            _create_thumbnails(partial_frames, store, job_id, cancel_event, deadline)
            if partial_frames:
                store.update(
                    job_id,
                    frames=all_frames + partial_frames,
                    stage=f"partial source {position} of {total_sources}",
                    progress=_source_progress(
                        position,
                        total_sources,
                        timestamp=partial_frames[-1].timestamp,
                        duration=source_record.duration,
                        frame_count=len(partial_frames),
                        frame_cap=remaining,
                    ),
                )
                _ensure_job_bytes(store, job_id, settings.max_job_bytes)
            raise

        _check_cancel(cancel_event, deadline)
        store.update(
            job_id,
            stage=f"creating thumbnails for source {position} of {total_sources}",
            progress=_source_progress(
                position,
                total_sources,
                timestamp=partial_frames[-1].timestamp if partial_frames else None,
                duration=source_record.duration,
                frame_count=len(partial_frames),
                frame_cap=remaining,
            ),
            frames=all_frames + partial_frames,
        )
        _create_thumbnails(partial_frames, store, job_id, cancel_event, deadline)
        store.update(
            job_id,
            frames=all_frames + partial_frames,
            progress=_source_progress(
                position,
                total_sources,
                timestamp=source_record.duration,
                duration=source_record.duration,
                frame_count=len(partial_frames),
                frame_cap=remaining,
            ),
        )
        _ensure_job_bytes(store, job_id, settings.max_job_bytes)
        return partial_frames
    finally:
        # Downloaded media and this job's uploads are disposable. External
        # authorized files remain owned by the person who supplied them.
        if remote_download:
            shutil.rmtree(source_work / "download", ignore_errors=True)
        elif media_path is not None:
            work_root = store.job_dir(job_id) / "work"
            try:
                resolved = media_path.resolve(strict=False)
                resolved.relative_to(work_root.resolve(strict=False))
            except ValueError:
                pass
            else:
                resolved.unlink(missing_ok=True)


def _create_thumbnails(
    frames: list[FrameMetadata],
    store: JobStore,
    job_id: str,
    cancel_event: Event,
    deadline: float | None,
) -> None:
    for frame in frames:
        _check_cancel(cancel_event, deadline)
        create_thumbnail(store.job_dir(job_id) / frame.path)
