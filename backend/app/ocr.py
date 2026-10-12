"""Optional local OCR adapter.

OCR is deliberately a subprocess adapter around the locally installed
Tesseract binary.  No paid or hosted OCR service is required, and disabled or
unavailable OCR is a harmless no-op.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from threading import Event
from typing import Protocol

from .processes import run_process


class OCRAdapter(Protocol):
    enabled: bool
    reason: str | None

    def extract(self, image_path: Path) -> str | None:
        ...


def normalize_ocr_text(text: str | None) -> str | None:
    """Return stable, human-readable OCR text for storage and comparisons.

    Tesseract can emit a mixture of compatibility characters, non-breaking
    spaces, and one whitespace character per visual line.  NFKC keeps the
    text searchable without making a semantic claim about it; collapsing
    whitespace makes harmless line wrapping noise irrelevant to the optional
    deduplication guard.
    """

    if text is None:
        return None
    normalized = unicodedata.normalize("NFKC", str(text))
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or None


def ocr_texts_conflict(left: str | None, right: str | None) -> bool:
    """Whether available OCR should protect a visually similar frame.

    OCR is intentionally only a guard.  When both frames have usable text,
    any normalized difference is treated conservatively as a possible title,
    equation, bullet, or digit change.  No semantic similarity or embedding
    is inferred here, and missing OCR never blocks visual matching.
    """

    normalized_left = normalize_ocr_text(left)
    normalized_right = normalize_ocr_text(right)
    if not normalized_left or not normalized_right:
        return False
    return normalized_left != normalized_right


# Small aliases make the helper convenient for callers while keeping the
# descriptive name above as the canonical API.
normalize_text = normalize_ocr_text
ocr_text_conflict = ocr_texts_conflict


class NoOpOCR:
    enabled = False

    def __init__(self, reason: str = "OCR disabled"):
        self.reason = reason

    def extract(self, image_path: Path) -> str | None:
        return None


class TesseractOCR:
    enabled = True

    def __init__(self, executable: str, timeout: int = 20, cancel_event: Event | None = None):
        self.executable = executable
        self.timeout = timeout
        self.cancel_event = cancel_event
        self.reason: str | None = None

    def extract(self, image_path: Path) -> str | None:
        try:
            result = run_process(
                [self.executable, str(image_path), "stdout", "--psm", "6"],
                timeout=self.timeout,
                cancel_event=self.cancel_event,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return normalize_ocr_text(result.stdout)


def create_ocr(enabled: bool, timeout: int = 20, cancel_event: Event | None = None) -> OCRAdapter:
    """Create Tesseract when requested and installed, otherwise a no-op."""

    if not enabled:
        return NoOpOCR()
    executable = shutil.which("tesseract")
    if not executable:
        return NoOpOCR("OCR requested but Tesseract is not installed")
    return TesseractOCR(executable, timeout=timeout, cancel_event=cancel_event)
