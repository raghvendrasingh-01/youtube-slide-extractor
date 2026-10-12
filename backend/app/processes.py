"""Cancellable subprocess execution shared by yt-dlp, ffmpeg and OCR."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from pathlib import Path
from threading import Event

from .errors import CancelledProcessing


def run_process(
    command: list[str],
    *,
    cancel_event: Event | None = None,
    timeout: float = 900,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Wait off the API loop, terminating the full child process group on cancel.

    ``communicate`` drains both pipes, including when a timeout occurs; using
    poll()+wait() with PIPE would deadlock on a verbose downloader.
    """
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("command must be a non-empty list of non-empty strings")
    if not math.isfinite(float(timeout)) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledProcessing("Job cancelled")
    popen_kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "cwd": cwd,
        "close_fds": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        command,
        **popen_kwargs,
    )
    started = time.monotonic()
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledProcessing("Job cancelled")
            if time.monotonic() - started >= timeout:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.communicate()


def process_error(error: Exception) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        return (error.stderr or error.stdout or str(error)).strip()[-2000:]
    if isinstance(error, subprocess.TimeoutExpired):
        return f"Operation exceeded its {error.timeout:g} second timeout"
    return str(error)
