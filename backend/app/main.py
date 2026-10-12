"""FastAPI application for the secure local FrameNotes lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from PIL import Image

from .config import AppSettings
from .errors import FrameNotesError, JobNotFound, SourceParseError
from .media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS, thumbnail_path
from .models import (
    AnalyzeRequest,
    BulkRetentionRequest,
    ExportRequest,
    FrameListResponse,
    FrameOrderRequest,
    FrameRetentionRequest,
    JobCreateRequest,
    JobRecord,
    ProgressEvent,
    SessionResponse,
    ExportAcceptedResponse,
)
from .processing import analyze_sources
from .security import (
    SESSION_COOKIE,
    SessionRateLimiter,
    SessionSigner,
    LocalRequestGuard,
    scrub_error,
    scrub_source,
    source_basename,
)
from .store import JobStore
from .urls import parse_sources
from .worker import JobManager, TERMINAL_STATUSES


class _BodyTooLarge(Exception):
    pass


def _public_record(record: JobRecord, *, include_frames: bool = True) -> dict:
    """Serialize a job without owner ids or server-local source paths."""

    payload = record.model_dump(mode="json")
    payload["error"] = scrub_error(payload.get("error"))
    request_payload = payload.get("request") or {}
    request_payload["sources"] = [scrub_source(value) for value in request_payload.get("sources", [])]
    payload["request"] = request_payload
    for source in payload.get("sources", []):
        local = source.get("kind") == "local"
        source["input"] = scrub_source(source.get("input", ""), local=local)
        source["canonical"] = scrub_source(source.get("canonical", ""), local=local)
        if local:
            source["title"] = source_basename(source.get("title", ""))
        source["error"] = scrub_error(source.get("error"))
    payload["warnings"] = [scrub_error(item) or "" for item in payload.get("warnings", [])]
    source_kinds = {source.get("id"): source.get("kind") for source in payload.get("sources", [])}
    for frame in payload.get("frames", []):
        # Frame URLs are the public artifact handles.  Returning only the
        # image basename avoids exposing the private job directory layout.
        frame["path"] = source_basename(str(frame.get("path", "")))
        if source_kinds.get(frame.get("source_id")) == "local":
            frame["source_title"] = source_basename(str(frame.get("source_title", "")))
    if not include_frames:
        payload["frames"] = []
    return payload


def _public_frame(record: JobRecord, frame):
    source = next((item for item in record.sources if item.id == frame.source_id), None)
    title = source_basename(frame.source_title) if source is not None and source.kind == "local" else frame.source_title
    return frame.model_copy(update={"path": source_basename(frame.path), "source_title": title})


def _summary(record: JobRecord) -> dict:
    payload = _public_record(record, include_frames=False)
    return {
        "id": payload["id"],
        "status": payload["status"],
        "progress": payload["progress"],
        "stage": payload["stage"],
        "error": payload["error"],
        "frame_count": payload["frame_count"],
        "duplicate_count": payload["duplicate_count"],
        "retained_count": payload["retained_count"],
        "ocr_status": payload["ocr_status"],
        "warnings": payload["warnings"],
        "techniques": payload["techniques"],
        "processing_seconds": payload["processing_seconds"],
        "expires_at": payload["expires_at"],
        "downloads": payload["downloads"],
    }


def _get_manager(request: Request) -> JobManager:
    return request.app.state.manager


def _http_error(error: Exception | str, status_code: int = 400) -> HTTPException:
    message = scrub_error(str(error)) or "Request could not be completed"
    return HTTPException(status_code=status_code, detail=message)


def _new_session(request: Request, *, create: bool = False) -> str:
    signer: SessionSigner = request.app.state.signer
    owner_id = signer.verify(request.cookies.get(SESSION_COOKIE))
    if owner_id:
        return owner_id
    if not create:
        raise _http_error("A valid FrameNotes session is required", 401)
    owner_id, token = signer.issue()
    request.state.framenotes_session_token = token
    return owner_id


def _expensive_session(request: Request, *, create: bool = False) -> str:
    owner_id = _new_session(request, create=create)
    settings: AppSettings = request.app.state.settings
    limiter: SessionRateLimiter = request.app.state.rate_limiter
    if not limiter.check(owner_id, settings.max_session_expensive_requests, settings.session_rate_window_seconds):
        raise _http_error("Too many expensive requests for this session", 429)
    return owner_id


def _owned_record(request: Request, job_id: str) -> JobRecord:
    owner_id = _new_session(request)
    try:
        record = _get_manager(request).get(job_id)
    except JobNotFound as exc:
        raise _http_error("job was not found", 404) from exc
    if record.owner_id != owner_id:
        # Deliberately do not distinguish a foreign id from an unknown id.
        raise _http_error("job was not found", 404)
    return record


def _apply_job_defaults(payload: JobCreateRequest, settings: AppSettings) -> JobCreateRequest:
    defaults = {}
    if "interval" not in payload.model_fields_set:
        defaults["interval"] = settings.default_interval
    if "start_time" not in payload.model_fields_set:
        defaults["start_time"] = settings.default_start_time
    if "max_frames" not in payload.model_fields_set:
        defaults["max_frames"] = settings.default_max_frames
    return payload.model_copy(update=defaults) if defaults else payload


def _recompute_source_counters(record: JobRecord) -> None:
    for source in record.sources:
        frames = [frame for frame in record.frames if frame.source_id == source.id]
        source.frame_count = len(frames)
        source.retained_count = sum(frame.retained for frame in frames)
        source.exact_removed = sum(frame.duplicate_reason == "exact_hash" for frame in frames)
        source.near_removed = sum(bool(frame.duplicate_of) and frame.duplicate_reason != "exact_hash" for frame in frames)


def _source_frame_order(record: JobRecord) -> dict[str, tuple[int, float, int, str]]:
    source_order = {source.id: (source.position or index) for index, source in enumerate(record.sources, start=1)}
    return {
        frame.id: (source_order.get(frame.source_id, 10**9), frame.timestamp, frame.index, frame.id)
        for frame in record.frames
    }


def _safe_download_path(store: JobStore, record: JobRecord, filename: str) -> Path:
    if Path(filename).name != filename or filename in {"", ".", ".."} or ".tmp." in filename:
        raise _http_error("invalid download filename", 400)
    artifact = next((item for item in record.downloads if item.filename == filename), None)
    if artifact is None:
        raise _http_error("download was not found", 404)
    try:
        downloads = store.downloads_dir(record.id)
    except JobNotFound as exc:
        raise _http_error("download was not found", 404) from exc
    path = (downloads / filename).resolve(strict=False)
    root = downloads.resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise _http_error("invalid download path", 400) from exc
    if path.is_symlink() or not path.is_file():
        raise _http_error("download was not found", 404)
    return path


def _safe_frame_path(store: JobStore, record: JobRecord, path_value: str) -> Path:
    base = store.job_dir(record.id).resolve(strict=False)
    path = (base / path_value).resolve(strict=False)
    try:
        frames_root = store.frames_dir(record.id).resolve(strict=False)
    except JobNotFound as exc:
        raise _http_error("frame image is no longer available", 404) from exc
    try:
        path.relative_to(base)
        path.relative_to(frames_root)
    except ValueError as exc:
        raise _http_error("invalid frame path", 400) from exc
    if path.is_symlink() or not path.is_file() or path.name.startswith(".") or ".tmp." in path.name:
        raise _http_error("frame image is no longer available", 404)
    return path


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Create an isolated app instance, convenient for tests and local use."""

    runtime_settings = settings or AppSettings()
    runtime_settings.ensure_directories()
    secure_cookies = runtime_settings.secure_cookies if runtime_settings.secure_cookie is None else runtime_settings.secure_cookie
    store = JobStore(runtime_settings.jobs_dir)
    manager = JobManager(store, runtime_settings)
    signer = SessionSigner(runtime_settings.jobs_dir, runtime_settings.session_secret, runtime_settings.session_secret_path)
    rate_limiter = SessionRateLimiter()
    analysis_capacity = asyncio.Semaphore(runtime_settings.max_concurrent_analyses)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cleanup_task = await manager.start_background_cleanup()
        yield
        await manager.shutdown(cleanup_task)

    app = FastAPI(
        title=runtime_settings.app_name,
        version="0.1.0",
        description="Local frame extraction, deduplication, and export API",
        lifespan=lifespan,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=runtime_settings.trusted_hosts)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Run before CORS/routing so untrusted origins and oversized JSON bodies
    # are rejected without invoking a handler.  Uploads remain streaming.
    runtime_settings.secure_cookies = bool(secure_cookies)
    app.add_middleware(LocalRequestGuard, settings=runtime_settings)

    app.state.settings = runtime_settings
    app.state.store = store
    app.state.manager = manager
    app.state.signer = signer
    app.state.rate_limiter = rate_limiter

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": runtime_settings.app_name}

    @app.get("/api/v1/session", response_model=SessionResponse)
    async def establish_session(request: Request) -> SessionResponse:
        _new_session(request, create=True)
        return SessionResponse()

    @app.post("/api/v1/analyze")
    async def analyze(payload: AnalyzeRequest, request: Request) -> dict:
        owner_id = _expensive_session(request, create=True)
        del owner_id
        try:
            async with analysis_capacity:
                results = await asyncio.to_thread(analyze_sources, payload.sources, runtime_settings)
            for item in results:
                local = item.get("kind") == "local"
                item["input"] = scrub_source(item.get("input", ""), local=local)
                item["canonical"] = scrub_source(item.get("canonical", ""), local=local)
                item["error"] = scrub_error(item.get("error"))
            return {"sources": results, "count": len(results)}
        except SourceParseError as exc:
            raise _http_error(exc, 422) from exc
        except FrameNotesError as exc:
            raise _http_error(exc, 400) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 429) from exc

    @app.post("/api/v1/jobs", status_code=202)
    async def create_job(payload: JobCreateRequest, request: Request) -> dict:
        owner_id = _expensive_session(request, create=True)
        try:
            parse_sources(payload.sources)
            payload = _apply_job_defaults(payload, runtime_settings)
            record = await manager.submit(payload, owner_id=owner_id)
            return _public_record(record)
        except SourceParseError as exc:
            raise _http_error(exc, 422) from exc
        except FrameNotesError as exc:
            raise _http_error(exc, 400) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 429) from exc

    @app.post("/api/v1/jobs/upload", status_code=202)
    async def upload_job(
        request: Request,
        filename: str = Query(..., min_length=1, max_length=255),
        config: str = Query(..., min_length=2, max_length=100_000),
    ) -> dict:
        owner_id = _expensive_session(request, create=True)
        if Path(filename).name != filename or filename in {".", ".."}:
            raise _http_error("invalid upload filename", 422)
        if any(ord(character) < 32 or ord(character) == 127 for character in filename):
            raise _http_error("invalid upload filename", 422)
        extension = Path(filename).suffix.lower()
        if extension not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            raise _http_error("unsupported upload file extension", 422)
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > runtime_settings.max_source_bytes:
                    raise _http_error("upload exceeds the source size limit", 413)
            except ValueError as exc:
                raise _http_error("invalid content length", 400) from exc
        try:
            parsed_config = json.loads(config)
            payload = JobCreateRequest.model_validate(parsed_config)
        except (ValueError, TypeError) as exc:
            raise _http_error("upload config must be valid JobCreateRequest JSON", 422) from exc
        if payload.sources != ["upload"]:
            raise _http_error("upload config sources must be ['upload']", 422)
        payload = _apply_job_defaults(payload, runtime_settings)

        # The user filename is used only to choose a fixed safe extension.
        # The worker never receives an attacker-controlled path or MIME type.
        # Allocate the private job first, then persist the absolute path only
        # inside the job document.  Public serializers reduce it to a basename.
        internal_request = payload.model_copy(update={"sources": ["upload"]})
        record = store.create(internal_request, owner_id=owner_id, retention_hours=runtime_settings.retention_hours)
        try:
            upload_path = store.work_dir(record.id) / f"upload{extension}"
        except Exception as exc:
            if store.exists(record.id):
                store.delete(record.id)
            raise _http_error(exc, 507) from exc
        internal_request = payload.model_copy(update={"sources": [str(upload_path)]})
        try:
            record = store.update(record.id, request=internal_request)
        except Exception as exc:
            if store.exists(record.id):
                store.delete(record.id)
            raise _http_error(exc, 507) from exc
        total = 0
        upload_deadline = time.monotonic() + runtime_settings.upload_timeout_seconds
        try:
            with upload_path.open("xb") as handle:
                async for chunk in request.stream():
                    if time.monotonic() >= upload_deadline:
                        raise TimeoutError("upload exceeded its time limit")
                    if await request.is_disconnected():
                        raise RuntimeError("upload was cancelled")
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > runtime_settings.max_source_bytes:
                        raise _BodyTooLarge
                    handle.write(chunk)
                handle.flush()
                handle_fd = handle.fileno()
                await asyncio.to_thread(os.fsync, handle_fd)
            if total <= 0:
                raise ValueError("upload body is empty")
            if extension in IMAGE_EXTENSIONS:
                try:
                    with Image.open(upload_path) as image:
                        image.verify()
                except Exception as exc:
                    raise ValueError("uploaded image could not be read") from exc
            started = await manager.start_prepared(record)
            return _public_record(started)
        except _BodyTooLarge as exc:
            upload_path.unlink(missing_ok=True)
            store.delete(record.id)
            raise _http_error("upload exceeds the source size limit", 413) from exc
        except asyncio.CancelledError:
            upload_path.unlink(missing_ok=True)
            if store.exists(record.id):
                store.delete(record.id)
            raise
        except TimeoutError as exc:
            upload_path.unlink(missing_ok=True)
            if store.exists(record.id):
                store.delete(record.id)
            raise _http_error(exc, 408) from exc
        except Exception as exc:
            upload_path.unlink(missing_ok=True)
            if store.exists(record.id):
                store.delete(record.id)
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, RuntimeError):
                raise _http_error(exc, 429) from exc
            raise _http_error(exc, 422) from exc

    @app.get("/api/v1/jobs")
    async def list_jobs(request: Request, full: bool = Query(False)) -> list[dict]:
        owner_id = _new_session(request)
        records = [record for record in manager.store.list_records() if record.owner_id == owner_id]
        records.sort(key=lambda record: record.created_at, reverse=True)
        if full:
            return [_public_record(record, include_frames=False) for record in records]
        return [_summary(record) for record in records]

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict:
        return _public_record(_owned_record(request, job_id))

    @app.get("/api/v1/jobs/{job_id}/progress")
    async def job_progress(job_id: str, request: Request) -> StreamingResponse:
        record = _owned_record(request, job_id)
        owner_id = record.owner_id

        async def events() -> AsyncIterator[str]:
            last_payload: str | None = None
            while True:
                if await request.is_disconnected():
                    break
                try:
                    current = manager.get(job_id)
                except JobNotFound:
                    break
                if current.owner_id != owner_id:
                    break
                event_type = "progress"
                if current.status == "completed":
                    event_type = "complete" if current.stage == "completed" else "stage"
                elif current.status == "cancelled":
                    event_type = "cancelled"
                elif current.status == "failed":
                    event_type = "error"
                elif current.status == "exporting":
                    event_type = "stage"
                event = ProgressEvent(
                    type=event_type,
                    job_id=current.id,
                    status=current.status,
                    progress=current.progress,
                    stage=current.stage,
                    frame_count=current.frame_count,
                    duplicate_count=current.duplicate_count,
                    retained_count=current.retained_count,
                    ocr_status=current.ocr_status,
                    error=scrub_error(current.error),
                    current_video=next(
                        (source_basename(source.title) if source.kind == "local" else source.title
                         for source in current.sources if source.status == "running"),
                        None,
                    ),
                    total_videos=len(current.sources),
                    completed_videos=sum(source.status == "completed" for source in current.sources),
                    warnings=[scrub_error(item) or "" for item in current.warnings],
                )
                payload = event.model_dump_json()
                if payload != last_payload:
                    yield f"event: progress\ndata: {payload}\n\n"
                    last_payload = payload
                # Exporting is intentionally not terminal; the same stream
                # reports the final exported/export-failed transition.
                if current.status in TERMINAL_STATUSES and current.stage not in {"export failed", "export cancelled"}:
                    break
                if current.status in {"completed", "cancelled"} and current.stage in {"export failed", "export cancelled"}:
                    break
                await asyncio.sleep(runtime_settings.worker_poll_seconds)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request) -> dict:
        _owned_record(request, job_id)
        try:
            return _public_record(await manager.cancel(job_id))
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc

    @app.get("/api/v1/jobs/{job_id}/frames", response_model=FrameListResponse)
    async def list_frames(
        job_id: str,
        request: Request,
        retained_only: bool = Query(False),
        include_duplicates: bool | None = Query(None),
        status: str | None = Query(None),
        search: str | None = Query(None, max_length=200),
        cursor: str | None = Query(None),
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> FrameListResponse:
        record = _owned_record(request, job_id)
        source_order = {source.id: (source.position or index) for index, source in enumerate(record.sources, start=1)}
        frames = sorted(
            record.frames,
            key=lambda frame: (
                source_order.get(frame.source_id, 10**9),
                frame.review_order if frame.review_order is not None else 10**9,
                frame.timestamp,
                frame.index,
                frame.id,
            ),
        )
        if cursor is not None:
            try:
                offset = max(0, int(cursor))
            except ValueError as exc:
                raise _http_error("cursor must be a non-negative integer", 400) from exc
        if retained_only or status == "retained":
            frames = [frame for frame in frames if frame.retained]
        elif status in {"discarded", "removed"}:
            frames = [frame for frame in frames if not frame.retained]
        if include_duplicates is False:
            frames = [frame for frame in frames if frame.duplicate_of is None]
        elif include_duplicates is True:
            frames = [frame for frame in frames if frame.duplicate_of is not None]
        if search:
            needle = search.casefold()
            source_by_id = {source.id: source for source in record.sources}
            frames = [
                frame
                for frame in frames
                if needle in (frame.ocr_text or "").casefold()
                or needle in frame.source_title.casefold()
                or needle in (source_by_id.get(frame.source_id).canonical.casefold() if source_by_id.get(frame.source_id) else "")
            ]
        total = len(frames)
        page = frames[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < total else None
        public_page = [_public_frame(record, frame) for frame in page]
        return FrameListResponse(frames=public_page, total=total, offset=offset, limit=limit, next_cursor=next_cursor)

    @app.get("/api/v1/jobs/{job_id}/duplicates", response_model=FrameListResponse)
    async def list_duplicates(
        job_id: str,
        request: Request,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
    ) -> FrameListResponse:
        record = _owned_record(request, job_id)
        source_order = {source.id: (source.position or index) for index, source in enumerate(record.sources, start=1)}
        frames = [frame for frame in record.frames if frame.duplicate_of is not None]
        frames.sort(key=lambda frame: (source_order.get(frame.source_id, 10**9), frame.timestamp, frame.index, frame.id))
        page = frames[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(frames) else None
        public_page = [_public_frame(record, frame) for frame in page]
        return FrameListResponse(frames=public_page, total=len(frames), offset=offset, limit=limit, next_cursor=next_cursor)

    async def _set_retention(job_id: str, frame_id: str, payload: FrameRetentionRequest, request: Request) -> dict:
        _owned_record(request, job_id)
        try:
            await manager.begin_edit(job_id)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 409) from exc
        updated_frame = None
        try:
            def mutate(record: JobRecord) -> None:
                nonlocal updated_frame
                for frame in record.frames:
                    if frame.id == frame_id:
                        frame.retained = payload.retained
                        frame.retention_overridden = True
                        _recompute_source_counters(record)
                        updated_frame = frame.model_copy(deep=True)
                        return
                raise KeyError(frame_id)

            record = store.mutate(job_id, mutate)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except KeyError as exc:
            raise _http_error(f"frame {exc.args[0]!r} was not found", 404) from exc
        finally:
            await manager.end_edit(job_id)
        response = _public_record(record)
        response["frame"] = _public_record(record)["frames"][next(index for index, frame in enumerate(record.frames) if frame.id == frame_id)] if updated_frame else None
        return response

    @app.patch("/api/v1/jobs/{job_id}/frames/{frame_id}/retention")
    async def patch_retention(job_id: str, frame_id: str, payload: FrameRetentionRequest, request: Request) -> dict:
        return await _set_retention(job_id, frame_id, payload, request)

    @app.patch("/api/v1/jobs/{job_id}/frames/order")
    async def reorder_frames(job_id: str, payload: FrameOrderRequest, request: Request) -> dict:
        _owned_record(request, job_id)
        try:
            await manager.begin_edit(job_id)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 409) from exc
        try:
            wanted = list(payload.frame_ids)

            def mutate(record: JobRecord) -> None:
                known = {frame.id for frame in record.frames}
                if len(set(wanted)) != len(wanted):
                    raise ValueError("frame_ids must not contain duplicates")
                unknown = [frame_id for frame_id in wanted if frame_id not in known]
                if unknown:
                    raise KeyError(unknown[0])
                stable = _source_frame_order(record)
                unspecified = [frame.id for frame in sorted(record.frames, key=lambda frame: stable[frame.id]) if frame.id not in wanted]
                for next_order, frame_id in enumerate(wanted + unspecified):
                    next(frame for frame in record.frames if frame.id == frame_id).review_order = next_order

            record = store.mutate(job_id, mutate)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except KeyError as exc:
            raise _http_error(f"frame {exc.args[0]!r} was not found", 404) from exc
        except ValueError as exc:
            raise _http_error(exc, 422) from exc
        finally:
            await manager.end_edit(job_id)
        return _public_record(record)

    @app.patch("/api/v1/jobs/{job_id}/frames/{frame_id}")
    async def patch_frame(job_id: str, frame_id: str, payload: FrameRetentionRequest, request: Request) -> dict:
        return await _set_retention(job_id, frame_id, payload, request)

    @app.post("/api/v1/jobs/{job_id}/frames/bulk")
    async def patch_frames_bulk(job_id: str, payload: BulkRetentionRequest, request: Request) -> dict:
        _owned_record(request, job_id)
        try:
            await manager.begin_edit(job_id)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 409) from exc
        changed = 0
        try:
            def mutate(record: JobRecord) -> None:
                nonlocal changed
                wanted = set(payload.frame_ids)
                known = {frame.id for frame in record.frames}
                unknown = wanted - known
                if unknown:
                    raise KeyError(next(iter(unknown)))
                for frame in record.frames:
                    if frame.id in wanted:
                        frame.retained = payload.status == "retained"
                        frame.retention_overridden = True
                        changed += 1
                _recompute_source_counters(record)

            record = store.mutate(job_id, mutate)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except KeyError as exc:
            raise _http_error(f"frame {exc.args[0]!r} was not found", 404) from exc
        finally:
            await manager.end_edit(job_id)
        return {"job": _public_record(record), "updated": changed}

    @app.post("/api/v1/jobs/{job_id}/export", status_code=202, response_model=ExportAcceptedResponse)
    async def export_job(job_id: str, payload: ExportRequest, request: Request) -> ExportAcceptedResponse:
        _expensive_session(request)
        _owned_record(request, job_id)
        try:
            record = await manager.start_export(job_id, payload)
            return ExportAcceptedResponse(job_id=record.id, status=record.status, downloads=[])
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except (FrameNotesError, RuntimeError) as exc:
            raise _http_error(exc, 409) from exc

    @app.get("/api/v1/jobs/{job_id}/downloads")
    async def list_downloads(job_id: str, request: Request) -> dict:
        record = _owned_record(request, job_id)
        return {"job_id": job_id, "downloads": [item.model_dump(mode="json") for item in record.downloads]}

    @app.get("/api/v1/jobs/{job_id}/exports")
    async def list_exports(job_id: str, request: Request) -> dict:
        record = _owned_record(request, job_id)
        artifacts = []
        for item in record.downloads:
            payload = item.model_dump(mode="json")
            payload["id"] = item.filename
            payload["download_url"] = item.download_url or f"/api/v1/jobs/{job_id}/downloads/{item.filename}"
            artifacts.append(payload)
        return {
            "job_id": job_id,
            "exports": artifacts,
            "summary": {
                "videos": len(record.sources),
                "frames_extracted": record.frame_count,
                "frames_retained": record.retained_count,
                "frames_discarded": record.duplicate_count,
                "ocr_completed": sum(bool(frame.ocr_text) for frame in record.frames),
            },
        }

    @app.get("/api/v1/jobs/{job_id}/downloads/{filename}")
    async def download_file(job_id: str, filename: str, request: Request) -> FileResponse:
        record = _owned_record(request, job_id)
        path = _safe_download_path(store, record, filename)
        return FileResponse(path, filename=filename)

    @app.get("/api/v1/jobs/{job_id}/exports/{filename}")
    async def download_export(job_id: str, filename: str, request: Request) -> FileResponse:
        return await download_file(job_id, filename, request)

    @app.get("/api/v1/jobs/{job_id}/frames/{frame_id}/image")
    async def frame_image(job_id: str, frame_id: str, request: Request) -> FileResponse:
        record = _owned_record(request, job_id)
        frame = next((item for item in record.frames if item.id == frame_id), None)
        if frame is None:
            raise _http_error("frame was not found", 404)
        path = _safe_frame_path(store, record, frame.path)
        return FileResponse(path)

    @app.get("/api/v1/jobs/{job_id}/frames/{frame_id}/thumbnail")
    async def frame_thumbnail(job_id: str, frame_id: str, request: Request) -> FileResponse:
        record = _owned_record(request, job_id)
        frame = next((item for item in record.frames if item.id == frame_id), None)
        if frame is None:
            raise _http_error("frame was not found", 404)
        full_path = _safe_frame_path(store, record, frame.path)
        thumbnail = thumbnail_path(full_path).resolve(strict=False)
        base = store.job_dir(record.id).resolve(strict=False)
        try:
            thumbnail.relative_to(base)
        except ValueError as exc:
            raise _http_error("invalid thumbnail path", 400) from exc
        if thumbnail.is_symlink() or not thumbnail.is_file():
            raise _http_error("frame thumbnail is no longer available", 404)
        return FileResponse(thumbnail)

    @app.get("/api/v1/downloads/{job_id}/{filename}")
    async def flat_download_file(job_id: str, filename: str, request: Request) -> FileResponse:
        return await download_file(job_id, filename, request)

    @app.delete("/api/v1/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str, request: Request) -> Response:
        _owned_record(request, job_id)
        try:
            await manager.delete(job_id)
        except JobNotFound as exc:
            raise _http_error("job was not found", 404) from exc
        except RuntimeError as exc:
            raise _http_error(exc, 409) from exc
        return Response(status_code=204)

    # Compatibility aliases for the existing browser client. Every aliased
    # endpoint points at the same ownership-checked handler.
    app.add_api_route("/api/jobs", create_job, methods=["POST"], status_code=202, include_in_schema=False)
    app.add_api_route("/api/jobs", list_jobs, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/upload", upload_job, methods=["POST"], status_code=202, include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}", get_job, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}", delete_job, methods=["DELETE"], status_code=204, include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/events", job_progress, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/v1/jobs/{job_id}/events", job_progress, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/progress", job_progress, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/cancel", cancel_job, methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames", list_frames, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/duplicates", list_duplicates, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/order", reorder_frames, methods=["PATCH"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/{frame_id}", patch_frame, methods=["PATCH"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/{frame_id}/retention", patch_retention, methods=["PATCH"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/{frame_id}/image", frame_image, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/{frame_id}/thumbnail", frame_thumbnail, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/frames/bulk", patch_frames_bulk, methods=["POST"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/export", export_job, methods=["POST"], status_code=202, include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/downloads", list_downloads, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/exports", list_exports, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/downloads/{filename}", download_file, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/jobs/{job_id}/exports/{filename}", download_export, methods=["GET"], include_in_schema=False)
    app.add_api_route("/api/downloads/{job_id}/{filename}", flat_download_file, methods=["GET"], include_in_schema=False)

    return app


app = create_app()
