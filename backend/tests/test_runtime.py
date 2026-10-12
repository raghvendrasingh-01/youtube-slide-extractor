from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from app.errors import CancelledProcessing
from app.processes import run_process


def test_cancellable_subprocess_terminates_its_process_group() -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.15, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CancelledProcessing):
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], cancel_event=cancel, timeout=5)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 5


def test_process_error_preserves_stderr() -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        run_process([sys.executable, "-c", "import sys; sys.stderr.write('source unavailable'); sys.exit(3)"])
    assert "source unavailable" in (caught.value.stderr or "")
