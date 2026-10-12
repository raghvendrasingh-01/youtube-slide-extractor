"""Bounded in-process workers for extraction and asynchronous exports."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

from .config import AppSettings
from .errors import CancelledProcessing, ExportError, JobNotFound
from .exports import create_exports
from .models import ExportRequest, JobCreateRequest, JobRecord, SourceRecord
from .processing import process_job
from .security import source_basename, scrub_error
from .store import JobStore


TERMINAL_STATUSES = {"completed", "cancelled", "failed"}
_EDITABLE_STATUSES = TERMINAL_STATUSES


class JobManager:
    """Own extraction tasks, export tasks, and their lifecycle races.

    CPU/video work runs in threads, while the event loop owns the small state
    transitions.  A job operation lock is deliberately fail-fast: retention,
    reorder, export, and delete never wait behind one another and therefore
    cannot overwrite a newer snapshot or remove a directory being written.
    """

    def __init__(self, store: JobStore, settings: AppSettings):
        self.store = store
        self.settings = settings
        self.tasks: dict[str, asyncio.Task[JobRecord | None]] = {}
        self.cancel_events: dict[str, Event] = {}
        self.export_tasks: dict[str, asyncio.Task[JobRecord | None]] = {}
        self.export_cancel_events: dict[str, Event] = {}
        self._busy: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._capacity = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._export_capacity = asyncio.Semaphore(settings.max_concurrent_exports)
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._lifespan_count = 0
        self._shutdown = False
        self.restored: list[JobRecord] = []
        self._restored = False

    async def start_background_cleanup(self) -> asyncio.Task[None]:
        # TestClient and local embedding code can open more than one lifespan
        # for the same app object.  Keep each loop's cleanup task local rather
        # than awaiting a task that belongs to another event loop.
        self._shutdown = False
        self._lifespan_count += 1
        if not self._restored:
            self.restored = await asyncio.to_thread(self._restore_state)
            self._restored = True
        task = asyncio.create_task(self._cleanup_loop(), name="framenotes-cleanup")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        return task

    def _restore_state(self) -> list[JobRecord]:
        restored = self.store.restore(self.settings.retention_hours)
        self.store.cleanup_orphans()
        return restored

    async def _cleanup_loop(self) -> None:
        try:
            while not self._shutdown:
                active = set(self.tasks) | set(self.export_tasks)
                await asyncio.to_thread(self.store.cleanup_expired, active)
                await asyncio.to_thread(self.store.cleanup_orphans)
                await asyncio.sleep(self.settings.cleanup_interval_seconds)
        except asyncio.CancelledError:
            raise

    def owner_job_count(self, owner_id: str) -> int:
        return sum(
            1
            for record in self.store.list_records()
            if record.owner_id == owner_id and record.status not in TERMINAL_STATUSES
        )

    async def submit(self, request: JobCreateRequest, owner_id: str = "legacy") -> JobRecord:
        async with self._lock:
            self._ensure_job_capacity(owner_id)
            record = self.store.create(request, owner_id=owner_id, retention_hours=self.settings.retention_hours)
            try:
                self._start_locked(record)
            except Exception:
                self.store.delete(record.id)
                raise
            return record.model_copy(deep=True)

    async def start_prepared(self, record: JobRecord) -> JobRecord:
        """Start a job whose upload has already been completely written."""

        async with self._lock:
            # The prepared record is already present in the store, so exclude
            # it from the per-session count used before normal creation.
            self._ensure_job_capacity(record.owner_id, ignore_job_id=record.id)
            try:
                self._start_locked(record)
            except Exception:
                self.store.delete(record.id)
                raise
            return self.store.get(record.id)

    def _ensure_job_capacity(self, owner_id: str, *, ignore_job_id: str | None = None) -> None:
        queued_records = self.store.list_records()
        queued_count = sum(record.status in {"queued", "running", "cancelling"} for record in queued_records)
        if ignore_job_id is not None:
            queued_count -= sum(record.id == ignore_job_id and record.status in {"queued", "running", "cancelling"} for record in queued_records)
        if len(self.tasks) >= self.settings.max_queued_jobs or queued_count >= self.settings.max_queued_jobs:
            raise RuntimeError(
                f"The local worker queue is full ({self.settings.max_queued_jobs} jobs). "
                "Wait for an existing job to finish and try again."
            )
        stored_records = queued_records
        stored_count = len(stored_records)
        if ignore_job_id is not None and any(record.id == ignore_job_id for record in stored_records):
            stored_count -= 1
        if stored_count >= self.settings.max_stored_jobs:
            raise RuntimeError("the local job retention limit has been reached")
        if self.store.disk_bytes() >= self.settings.max_storage_bytes:
            raise RuntimeError("the local storage limit has been reached")
        owner_count = self.owner_job_count(owner_id)
        if ignore_job_id is not None:
            try:
                ignored = self.store.get(ignore_job_id)
            except JobNotFound:
                ignored = None
            if ignored is not None and ignored.owner_id == owner_id and ignored.status not in TERMINAL_STATUSES:
                owner_count -= 1
        if owner_count >= self.settings.max_session_jobs:
            raise RuntimeError("This browser session has reached its active job limit")

    def _start_locked(self, record: JobRecord) -> None:
        event = Event()
        self.store.pin(record.id)
        self.cancel_events[record.id] = event
        try:
            self.tasks[record.id] = asyncio.create_task(self._run(record.id, event), name=f"framenotes-job-{record.id}")
        except Exception:
            self.cancel_events.pop(record.id, None)
            self.store.unpin(record.id)
            raise

    async def _run(self, job_id: str, cancel_event: Event) -> JobRecord | None:
        try:
            async with self._capacity:
                return await asyncio.to_thread(process_job, job_id, self.store, self.settings, cancel_event)
        except Exception as exc:
            # A delete may race a worker finishing its final write. Preserve
            # responsiveness without resurrecting deleted state.
            if self.store.exists(job_id):
                return self.store.update(
                    job_id,
                    status="failed",
                    stage="failed",
                    error=scrub_error(str(exc) or exc.__class__.__name__),
                )
            return None
        finally:
            async with self._lock:
                self.tasks.pop(job_id, None)
                self.cancel_events.pop(job_id, None)
                self.store.unpin(job_id)

    def get(self, job_id: str) -> JobRecord:
        return self.store.get(job_id)

    def require_editable(self, job_id: str) -> JobRecord:
        record = self.store.get(job_id)
        if record.status not in _EDITABLE_STATUSES:
            raise RuntimeError("job edits are available only after extraction has reached a terminal state")
        if job_id in self.tasks or job_id in self.export_tasks or job_id in self._busy:
            raise RuntimeError("job is busy")
        return record

    async def begin_edit(self, job_id: str) -> JobRecord:
        async with self._lock:
            record = self.require_editable(job_id)
            self._busy[job_id] = "edit"
            try:
                self.store.pin(job_id)
            except Exception:
                self._busy.pop(job_id, None)
                raise
            return record

    async def end_edit(self, job_id: str) -> None:
        async with self._lock:
            if self._busy.get(job_id) == "edit":
                self._busy.pop(job_id, None)
                self.store.unpin(job_id)

    async def cancel(self, job_id: str) -> JobRecord:
        async with self._lock:
            record = self.store.get(job_id)
            export_event = self.export_cancel_events.get(job_id)
            if export_event is not None:
                export_event.set()
                # Extraction frames remain intact.  The export task will write
                # the final cancelled snapshot when its thread observes this.
                return self.store.update(job_id, status="cancelled", stage="export cancelled", error=None)
            if record.status in TERMINAL_STATUSES:
                return record
            event = self.cancel_events.get(job_id)
            if event is not None:
                event.set()
                return self.store.update(job_id, status="cancelling", stage="cancelling")
            return self.store.update(
                job_id,
                status="cancelled",
                stage="cancelled",
                error="Job cancelled before a worker was available",
            )

    async def wait(self, job_id: str, timeout: float | None = None) -> JobRecord:
        task = self.tasks.get(job_id) or self.export_tasks.get(job_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return self.store.get(job_id)

    async def delete(self, job_id: str) -> None:
        process_task = self.tasks.get(job_id)
        export_task = self.export_tasks.get(job_id)
        if process_task is not None:
            event = self.cancel_events.get(job_id)
            if event is not None:
                event.set()
        if export_task is not None:
            event = self.export_cancel_events.get(job_id)
            if event is not None:
                event.set()

        pending: list[asyncio.Task[JobRecord | None]] = [
            task for task in (process_task, export_task) if task is not None and not task.done()
        ]
        if pending:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*pending, return_exceptions=True)), timeout=self.settings.delete_wait_seconds)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("job is still active; refusing to remove its directory") from exc
        if any(not task.done() for task in (process_task, export_task) if task is not None):
            raise RuntimeError("job is still active; refusing to remove its directory")
        async with self._lock:
            if job_id in self.tasks or job_id in self.export_tasks or job_id in self._busy:
                raise RuntimeError("job is still active; refusing to remove its directory")
            self.store.delete(job_id)

    async def start_export(self, job_id: str, request: ExportRequest) -> JobRecord:
        async with self._lock:
            record = self.store.get(job_id)
            if record.status not in _EDITABLE_STATUSES:
                raise RuntimeError("job must finish before it can be exported")
            if job_id in self._busy or job_id in self.tasks or job_id in self.export_tasks:
                raise RuntimeError("job is busy")
            if len(self.export_tasks) >= self.settings.max_queued_exports:
                raise RuntimeError("the local export queue is full")
            if record.export_count >= self.settings.max_exports_per_job:
                raise RuntimeError("this job has reached its export limit")
            event = Event()
            self._busy[job_id] = "export"
            self.export_cancel_events[job_id] = event
            self.store.pin(job_id)
            try:
                status = self.store.update(
                    job_id,
                    status="exporting",
                    stage="exporting " + ", ".join(request.formats),
                    error=None,
                )
            except Exception:
                self.export_cancel_events.pop(job_id, None)
                self._busy.pop(job_id, None)
                self.store.unpin(job_id)
                raise
            snapshot = status.model_copy(deep=True)
            try:
                task = asyncio.create_task(
                    self._run_export(job_id, snapshot, request, event),
                    name=f"framenotes-export-{job_id}",
                )
            except Exception:
                self.export_cancel_events.pop(job_id, None)
                self._busy.pop(job_id, None)
                self.store.unpin(job_id)
                self.store.update(job_id, status=record.status, stage=record.stage, error=record.error)
                raise
            self.export_tasks[job_id] = task
            return status

    async def export(self, job_id: str, request: ExportRequest) -> JobRecord:
        """Compatibility entry point; export work is intentionally asynchronous."""

        return await self.start_export(job_id, request)

    async def _run_export(
        self,
        job_id: str,
        record: JobRecord,
        request: ExportRequest,
        cancel_event: Event,
    ) -> JobRecord | None:
        acquired = False
        artifacts = []
        try:
            while not acquired:
                if cancel_event.is_set():
                    raise CancelledProcessing("Export cancelled by user")
                try:
                    await asyncio.wait_for(self._export_capacity.acquire(), timeout=0.1)
                    acquired = True
                except asyncio.TimeoutError:
                    continue
            export_record = _safe_export_snapshot(record)
            artifacts = await asyncio.to_thread(
                create_exports,
                export_record,
                self.store.job_dir(job_id),
                request,
                cancel_event,
            )
            if cancel_event.is_set():
                raise CancelledProcessing("Export cancelled by user")
            total_bytes = sum(int(item.size_bytes) for item in artifacts)
            current = self.store.get(job_id)
            actual_export_bytes = self.store.disk_bytes(job_id, downloads_only=True)
            if current.export_bytes + total_bytes > self.settings.max_export_bytes or actual_export_bytes > self.settings.max_export_bytes:
                raise ExportError("the job export disk limit has been reached")
            _validate_published_artifacts(artifacts)

            def finish(updated: JobRecord) -> None:
                merged = {item.filename: item for item in updated.downloads}
                for artifact in artifacts:
                    artifact.download_url = f"/api/v1/jobs/{job_id}/downloads/{artifact.filename}"
                    merged[artifact.filename] = artifact
                updated.downloads = list(merged.values())
                updated.status = "completed"
                updated.stage = "exported"
                updated.progress = 100.0
                updated.error = None
                updated.export_count += 1
                updated.export_bytes = max(updated.export_bytes + total_bytes, actual_export_bytes)

            return self.store.mutate(job_id, finish)
        except CancelledProcessing as exc:
            _remove_artifacts(self.store, job_id, artifacts)
            if self.store.exists(job_id):
                return self.store.update(job_id, status="cancelled", stage="export cancelled", error=str(exc))
            return None
        except Exception as exc:
            _remove_artifacts(self.store, job_id, artifacts)
            if self.store.exists(job_id):
                # An export failure does not destroy extraction results.
                return self.store.update(
                    job_id,
                    status="completed",
                    stage="export failed",
                    error=scrub_error(str(exc) or exc.__class__.__name__),
                )
            return None
        finally:
            if acquired:
                self._export_capacity.release()
            async with self._lock:
                self.export_tasks.pop(job_id, None)
                self.export_cancel_events.pop(job_id, None)
                if self._busy.get(job_id) == "export":
                    self._busy.pop(job_id, None)
                    self.store.unpin(job_id)

    async def shutdown(self, cleanup_task: asyncio.Task[None] | None = None) -> None:
        if cleanup_task is not None and not cleanup_task.done():
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        self._lifespan_count = max(0, self._lifespan_count - 1)
        if self._lifespan_count:
            return
        self._shutdown = True
        for event in list(self.cancel_events.values()) + list(self.export_cancel_events.values()):
            event.set()
        tasks = list(self.tasks.values()) + list(self.export_tasks.values())
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=self.settings.shutdown_wait_seconds)
            except asyncio.TimeoutError:
                # The underlying worker thread may still be finishing a
                # subprocess.  Do not delete its directory during shutdown.
                for task in tasks:
                    if not task.done():
                        task.cancel()


def _safe_export_snapshot(record: JobRecord) -> JobRecord:
    """Prevent generated documents from embedding server-local source paths."""

    snapshot = record.model_copy(deep=True)
    sources: list[SourceRecord] = []
    for source in snapshot.sources:
        if source.kind == "local":
            source.input = source_basename(source.input)
            source.canonical = source_basename(source.canonical)
            if "/" in source.title or "\\" in source.title:
                source.title = source_basename(source.title)
        sources.append(source)
    snapshot.sources = sources
    snapshot.request = snapshot.request.model_copy(
        update={
            "sources": [
                source_basename(value) if not str(value).lower().startswith(("http://", "https://")) else str(value)
                for value in snapshot.request.sources
            ]
        }
    )
    return snapshot


def _validate_published_artifacts(artifacts) -> None:
    for artifact in artifacts:
        filename = str(artifact.filename)
        if Path(filename).name != filename or filename in {"", ".", ".."} or ".tmp." in filename:
            raise ExportError("export produced an invalid artifact name")


def _remove_artifacts(store: JobStore, job_id: str, artifacts) -> None:
    if not artifacts:
        return
    try:
        directory = store.downloads_dir(job_id)
    except JobNotFound:
        return
    for artifact in artifacts:
        filename = str(getattr(artifact, "filename", ""))
        if Path(filename).name == filename and filename not in {"", ".", ".."}:
            (directory / filename).unlink(missing_ok=True)
