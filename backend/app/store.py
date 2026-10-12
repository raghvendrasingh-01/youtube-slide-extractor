"""Small file-backed job store used by the in-process worker.

The store deliberately uses one JSON document per job.  It is enough for the
local MVP, survives an API restart, and keeps the worker independent from a
database service while still making job state inspectable by a user.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import timedelta
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .errors import JobNotFound
from .models import JobCreateRequest, JobRecord, utc_now


class JobStore:
    """Thread-safe JSON persistence and isolated job directory management."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[tuple[int, int, int], JobRecord]] = {}
        # Manager pins are checked under the same lock as cleanup; a stale
        # periodic snapshot cannot delete a job just starting an export/edit.
        self._active: set[str] = set()

    def pin(self, job_id: str) -> None:
        with self._lock:
            self.get(job_id)
            self._active.add(job_id)

    def unpin(self, job_id: str) -> None:
        with self._lock:
            self._active.discard(job_id)

    def job_dir(self, job_id: str) -> Path:
        """Return a job directory without creating it."""

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", job_id):
            raise JobNotFound(f"job {job_id!r} was not found")
        directory = self.root / job_id
        if directory.is_symlink():
            raise JobNotFound(f"job {job_id!r} was not found")
        return directory

    def work_dir(self, job_id: str) -> Path:
        return self._private_subdir(job_id, "work")

    def frames_dir(self, job_id: str) -> Path:
        return self._private_subdir(job_id, "frames")

    def downloads_dir(self, job_id: str) -> Path:
        return self._private_subdir(job_id, "downloads")

    def _private_subdir(self, job_id: str, name: str) -> Path:
        self.get(job_id)
        base = self.job_dir(job_id).resolve(strict=False)
        path = base / name
        if path.is_symlink():
            raise JobNotFound(f"job {job_id!r} was not found")
        path.mkdir(parents=True, exist_ok=True)
        if path.resolve(strict=False).parent != base:
            raise JobNotFound(f"job {job_id!r} was not found")
        return path

    def _document_path(self, job_id: str) -> Path:
        path = self.job_dir(job_id) / "job.json"
        if path.is_symlink():
            raise JobNotFound("job was not found")
        return path

    def create(self, request: JobCreateRequest, owner_id: str = "legacy", retention_hours: int = 24) -> JobRecord:
        with self._lock:
            for _ in range(5):
                job_id = uuid4().hex
                directory = self.job_dir(job_id)
                try:
                    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
                except FileExistsError:
                    continue
                (directory / "frames").mkdir()
                (directory / "work").mkdir()
                (directory / "downloads").mkdir()
                now = utc_now()
                record = JobRecord(
                    id=job_id,
                    owner_id=owner_id,
                    request=request,
                    created_at=now,
                    updated_at=now,
                    expires_at=now + timedelta(hours=max(1, int(retention_hours))),
                )
                try:
                    self._write_locked(record)
                except Exception:
                    shutil.rmtree(directory, ignore_errors=True)
                    raise
                return record.model_copy(deep=True)
            raise RuntimeError("could not allocate a unique job directory")

    def exists(self, job_id: str) -> bool:
        try:
            return self._document_path(job_id).is_file()
        except JobNotFound:
            return False

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            path = self._document_path(job_id)
            if not path.is_file():
                raise JobNotFound(f"job {job_id!r} was not found")
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except OSError as exc:
                raise RuntimeError(f"job {job_id!r} has unreadable state") from exc
            cached = self._cache.get(job_id)
            if cached is not None and cached[0] == signature:
                return cached[1].model_copy(deep=True)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                record = JobRecord.model_validate(data)
                if record.id != job_id:
                    raise ValueError("job identity mismatch")
                self._cache[job_id] = (signature, record.model_copy(deep=True))
                return record.model_copy(deep=True)
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"job {job_id!r} has unreadable state") from exc

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        """Apply field changes and persist an atomic snapshot."""

        with self._lock:
            current = self.get(job_id)
            for key, value in changes.items():
                if not hasattr(current, key):
                    raise AttributeError(f"unknown job field: {key}")
                setattr(current, key, value)
            current.updated_at = utc_now()
            self._write_locked(current)
            return current.model_copy(deep=True)

    def mutate(self, job_id: str, callback: Callable[[JobRecord], None]) -> JobRecord:
        """Mutate a record under one lock, useful for frame/retention edits."""

        with self._lock:
            current = self.get(job_id)
            callback(current)
            current.updated_at = utc_now()
            self._write_locked(current)
            return current.model_copy(deep=True)

    def _write_locked(self, record: JobRecord) -> None:
        path = self._document_path(record.id)
        if not path.parent.is_dir():
            raise JobNotFound(f"job {record.id!r} was not found")
        payload_data = record.model_dump(mode="json")
        # owner_id/export counters are intentionally excluded from public
        # model dumps, but are required to restore the private lifecycle state.
        payload_data["owner_id"] = record.owner_id
        payload_data["export_count"] = record.export_count
        payload_data["export_bytes"] = record.export_bytes
        for frame, stored_frame in zip(record.frames, payload_data["frames"]):
            stored_frame["path"] = frame.path
        payload = json.dumps(payload_data, indent=2, sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix="job-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                # Windows and a few restricted filesystems do not expose a
                # directory fd.  The atomic replace still protects readers.
                pass
            try:
                stat = path.stat()
                self._cache[record.id] = ((stat.st_mtime_ns, stat.st_size, stat.st_ino), record.model_copy(deep=True))
            except OSError:
                self._cache.pop(record.id, None)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def list_records(self) -> list[JobRecord]:
        with self._lock:
            records: list[JobRecord] = []
            for path in sorted(self.root.glob("*/job.json")):
                try:
                    records.append(self.get(path.parent.name))
                except (JobNotFound, RuntimeError):
                    continue
            return records

    def restore(self, retention_hours: int = 24) -> list[JobRecord]:
        """Load persisted jobs and mark interrupted active work as failed."""

        restored: list[JobRecord] = []
        with self._lock:
            for record in self.list_records():
                if record.id in self._active:
                    continue
                changed = False
                if record.expires_at is None:
                    record.expires_at = record.created_at + timedelta(hours=retention_hours)
                    changed = True
                if record.status in {"queued", "running", "cancelling"}:
                    exporting = record.stage in {"cancelling export", "export queued"} or record.stage.startswith("exporting")
                    record.status = "completed" if exporting else "failed"
                    record.stage = "export failed" if exporting else "interrupted"
                    record.error = "The worker stopped before this job finished."
                    for source in record.sources:
                        if source.status in {"queued", "running"}:
                            source.status = "failed"
                            source.error = record.error
                    changed = True
                elif record.status == "exporting":
                    # Extraction is already terminal; leave its frames
                    # reviewable and make the interrupted export retryable.
                    record.status = "completed"
                    record.stage = "export failed"
                    record.error = "The export worker stopped before publishing artifacts."
                    changed = True
                if changed:
                    record.updated_at = utc_now()
                    self._write_locked(record)
                restored.append(record.model_copy(deep=True))
                self.cleanup_work(record.id)
                self.cleanup_unpublished(record.id)
                for temporary in self.job_dir(record.id).glob("job-*.json"):
                    temporary.unlink(missing_ok=True)
        return restored

    def cleanup_work(self, job_id: str) -> None:
        """Downloaded media is disposable; only frame images/exports are retained."""
        with self._lock:
            directory = self.job_dir(job_id) / "work"
            if directory.is_symlink():
                directory.unlink()
            elif directory.exists():
                shutil.rmtree(directory)
            if self.exists(job_id):
                directory.mkdir(exist_ok=True)

    def delete(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._active:
                raise RuntimeError("job is still active; refusing to remove its directory")
            directory = self.job_dir(job_id)
            if not directory.exists():
                raise JobNotFound(f"job {job_id!r} was not found")
            # The path is always rooted under the configured store.  Resolve
            # once more to prevent accidental traversal if a caller supplies a
            # malformed id.
            resolved = directory.resolve(strict=False)
            if resolved.parent != self.root.resolve(strict=False):
                raise ValueError("refusing to delete a job outside the store")
            shutil.rmtree(resolved)
            self._cache.pop(job_id, None)

    def replace_record(self, record: JobRecord) -> JobRecord:
        """Persist a complete record, primarily useful to worker tests."""

        with self._lock:
            if not self.job_dir(record.id).exists():
                raise JobNotFound(f"job {record.id!r} was not found")
            record.updated_at = utc_now()
            self._write_locked(record)
            return deepcopy(record)

    def cleanup_orphans(self) -> None:
        """Remove incomplete directories left by an aborted allocation."""

        with self._lock:
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    continue
                if directory.name in self._active:
                    continue
                try:
                    self.get(directory.name)
                except (JobNotFound, RuntimeError):
                    pass
                else:
                    continue
                shutil.rmtree(directory, ignore_errors=True)

    def cleanup_expired(self, active_ids: set[str] | None = None) -> list[str]:
        """Delete expired terminal jobs, never touching active worker ids."""

        active = active_ids or set()
        removed: list[str] = []
        now = utc_now()
        with self._lock:
            for record in self.list_records():
                if record.id in active or record.id in self._active or record.status in {"queued", "running", "cancelling", "exporting"}:
                    continue
                if record.expires_at is None or record.expires_at > now:
                    continue
                directory = self.job_dir(record.id).resolve(strict=False)
                if directory.parent != self.root.resolve(strict=False):
                    continue
                shutil.rmtree(directory, ignore_errors=True)
                self._cache.pop(record.id, None)
                removed.append(record.id)
        return removed

    def cleanup_unpublished(self, job_id: str) -> None:
        """Remove only files absent from a finished job's artifact manifest."""

        with self._lock:
            record = self.get(job_id)
            keep = {artifact.filename for artifact in record.downloads}
            directory = self.job_dir(job_id) / "downloads"
            if directory.is_symlink():
                directory.unlink()
                return
            if not directory.is_dir():
                return
            for path in directory.iterdir():
                if path.name not in keep or path.is_symlink():
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)

    def disk_bytes(self, job_id: str | None = None, *, downloads_only: bool = False) -> int:
        """Measured on-disk size; ignore symlinks and tolerate atomic replaces."""
        with self._lock:
            root = self.job_dir(job_id) if job_id is not None else self.root
            if downloads_only:
                root = root / "downloads"
            total = 0
            for directory, subdirectories, files in os.walk(root, followlinks=False):
                subdirectories[:] = [name for name in subdirectories if not (Path(directory) / name).is_symlink()]
                for name in files:
                    path = Path(directory) / name
                    try:
                        if not path.is_symlink():
                            total += path.stat().st_size
                    except FileNotFoundError:
                        continue
            return total
