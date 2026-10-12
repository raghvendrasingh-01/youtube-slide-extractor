"""Incremental visual frame deduplication.

The matcher deliberately has three stages:

* a decoded-pixel SHA-256 map handles exact visual repeats in O(1),
* a BK-tree indexes pHash values so near candidates do not require an
  all-pairs scan, and
* SSIM plus conservative local/high-contrast and optional OCR guards decide
  whether a candidate is safe to mark as a duplicate.

Representative pixel arrays are kept in a small LRU cache.  A cache miss
reloads the job-relative image, so a long playlist does not retain every
full-resolution or comparison image in memory.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Iterable

import imagehash
import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity

from .errors import CancelledProcessing
from .models import DeduplicationMode, FrameMetadata
from .ocr import ocr_texts_conflict


@dataclass(frozen=True)
class DedupeThresholds:
    phash_distance: int
    ssim: float
    changed_fraction: float


# The pHash distance is overridden by the constructor's default of six.  The
# mode-specific values remain useful documentation and provide conservative
# visual thresholds for each mode.
THRESHOLDS: dict[DeduplicationMode, DedupeThresholds] = {
    "strict": DedupeThresholds(6, 0.992, 0.010),
    "balanced": DedupeThresholds(6, 0.975, 0.035),
    "aggressive": DedupeThresholds(6, 0.940, 0.080),
    "manual": DedupeThresholds(6, 0.975, 0.035),
    "off": DedupeThresholds(0, 1.1, 0.0),
}


@dataclass(frozen=True)
class ImageSignature:
    """A visual signature and a bounded comparison-sized pixel sample."""

    exact_hash: str
    perceptual_hash: str
    pixels: np.ndarray | None
    aspect_ratio: float
    width: int = 0
    height: int = 0
    path: Path | None = None


@dataclass(frozen=True)
class _StoredSignature:
    exact_hash: str
    perceptual_hash: str
    aspect_ratio: float
    width: int
    height: int
    path: Path


class _BKNode:
    __slots__ = ("key", "values", "children")

    def __init__(self, key: str, value: str):
        self.key = key
        self.values: list[str] = [value]
        self.children: dict[int, _BKNode] = {}


class _BKTree:
    """Small Hamming-distance BK-tree for hexadecimal pHash strings."""

    def __init__(self) -> None:
        self.root: _BKNode | None = None

    @staticmethod
    def distance(left: str, right: str) -> int:
        return (int(left, 16) ^ int(right, 16)).bit_count()

    def add(self, key: str, value: str) -> None:
        if self.root is None:
            self.root = _BKNode(key, value)
            return
        node = self.root
        while True:
            distance = self.distance(key, node.key)
            if distance == 0:
                if value not in node.values:
                    node.values.append(value)
                return
            child = node.children.get(distance)
            if child is None:
                node.children[distance] = _BKNode(key, value)
                return
            node = child

    def search(self, key: str, maximum_distance: int) -> list[tuple[int, str]]:
        if self.root is None:
            return []
        result: list[tuple[int, str]] = []
        stack = [self.root]
        while stack:
            node = stack.pop()
            distance = self.distance(key, node.key)
            if distance <= maximum_distance:
                result.extend((distance, value) for value in node.values)
            lower = max(0, distance - maximum_distance)
            upper = distance + maximum_distance
            stack.extend(child for edge, child in node.children.items() if lower <= edge <= upper)
        return result


def _resolve_image_path(path: Path | str, root_dir: Path | str | None = None) -> Path:
    candidate = Path(path)
    if root_dir is not None and not candidate.is_absolute():
        candidate = Path(root_dir) / candidate
    return candidate


def image_signature(path: Path | str, root_dir: Path | str | None = None) -> ImageSignature:
    """Read a frame and produce a decoded-pixel SHA-256 and pHash.

    Hashing decoded RGB pixels rather than the encoded file bytes makes a
    lossless re-save an exact visual match while still letting re-encoded or
    lightly compressed frames reach the indexed perceptual stage.
    """

    candidate = _resolve_image_path(path, root_dir)
    with Image.open(candidate) as image:
        rgb = image.convert("RGB")
        original_pixels = np.asarray(rgb)
        digest = hashlib.sha256(
            f"{rgb.width}x{rgb.height}:".encode("ascii") + original_pixels.tobytes()
        ).hexdigest()
        phash = str(imagehash.phash(rgb))
        comparison_image = rgb.copy()
        comparison_image.thumbnail((384, 384), Image.Resampling.LANCZOS)
        comparison = np.asarray(comparison_image).copy()
        width, height = rgb.size
    return ImageSignature(
        digest,
        phash,
        comparison,
        width / height if height else 0.0,
        width,
        height,
        candidate,
    )


def _phash_distance(left: str, right: str) -> int:
    return _BKTree.distance(left, right)


def _resize_like(image: np.ndarray, shape: tuple[int, int, int]) -> np.ndarray:
    if image.shape == shape:
        return image
    return np.asarray(
        Image.fromarray(image).resize((shape[1], shape[0]), Image.Resampling.LANCZOS)
    )


def _ssim(left: np.ndarray, right: np.ndarray) -> float:
    right = _resize_like(right, left.shape)
    window = min(7, left.shape[0], left.shape[1])
    if window % 2 == 0:
        window -= 1
    if window < 3:
        return 0.0  # Tiny images only participate in exact matching.
    return float(
        structural_similarity(
            left,
            right,
            data_range=255,
            channel_axis=2,
            win_size=window,
        )
    )


def _local_change_guard(left: np.ndarray, right: np.ndarray, changed_fraction: float) -> bool:
    """Return True when high-contrast changes look like meaningful local text.

    A broad crop/edge shift can have many changed pixels without being a new
    slide, so the guard only protects compact or moderately compact regions.
    This catches a new equation, bullet, digit, or annotation while allowing
    the larger low-detail border differences common in video captures.
    """

    right = _resize_like(right, left.shape)
    difference = np.max(
        np.abs(left.astype(np.int16) - right.astype(np.int16)),
        axis=2,
    )
    changed = difference >= 18
    high_contrast = difference >= 36
    total = changed.size
    changed_ratio = float(np.mean(changed))
    if changed_ratio > changed_fraction:
        ys, xs = np.nonzero(changed)
        bbox_fraction = float((xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1) / total) if len(xs) else 1.0
        # Broad edge/crop changes are common in a video capture and should
        # continue to the SSIM gate.  Compact changes over the mode's area
        # budget are protected immediately.
        if bbox_fraction <= 0.45 or changed_ratio > max(changed_fraction * 8, 0.08):
            return True

    count = int(np.count_nonzero(high_contrast))
    minimum = max(6, int(total * 0.00005))
    if count < minimum:
        return False
    ys, xs = np.nonzero(high_contrast)
    if not len(xs):
        return False
    bbox_fraction = float((xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1) / total)

    # A compact high-contrast region is the characteristic shape of changed
    # text.  A broad region is usually a transition, crop, or lighting change;
    # its SSIM and changed-ratio thresholds still decide that case.
    if bbox_fraction <= 0.20:
        return True
    if bbox_fraction <= 0.45:
        padded = np.pad(high_contrast, 4, mode="constant")
        integral = padded.cumsum(axis=0).cumsum(axis=1)
        window_sum = (
            integral[9:, 9:]
            - integral[:-9, 9:]
            - integral[9:, :-9]
            + integral[:-9, :-9]
        )
        if int(window_sum.max(initial=0)) >= 4:
            return True
    return False


class FrameDeduplicator:
    """Incrementally annotate exact and near duplicate frame suspects."""

    # Comparison arrays are at most 384x384x3.  Keeping 64 arrays bounds the
    # large part of signature memory while the BK-tree retains only hashes and
    # frame ids for the whole job.
    _PIXEL_CACHE_SIZE = 64

    def __init__(
        self,
        mode: DeduplicationMode = "balanced",
        root_dir: Path | str | None = None,
        cancel_event: Event | None = None,
        phash_distance: int | None = None,
    ):
        if mode not in THRESHOLDS:
            raise ValueError(f"unsupported deduplication mode: {mode}")
        if phash_distance is not None and not 0 <= int(phash_distance) <= 64:
            raise ValueError("phash_distance must be between 0 and 64")
        self.mode = mode
        self.root_dir = root_dir
        self.cancel_event = cancel_event
        self.phash_distance = 6 if phash_distance is None else int(phash_distance)
        mode_thresholds = THRESHOLDS[mode]
        self.thresholds = DedupeThresholds(
            self.phash_distance,
            mode_thresholds.ssim,
            mode_thresholds.changed_fraction,
        )

        # ``representatives`` remains a useful public inspection surface, but
        # its ImageSignature values intentionally have no pixel arrays.
        self.representatives: list[tuple[FrameMetadata, ImageSignature]] = []
        self.exact: dict[str, FrameMetadata] = {}
        self._frames_by_id: dict[str, FrameMetadata] = {}
        self._records: dict[str, _StoredSignature] = {}
        self._order: dict[str, int] = {}
        self._phash_index = _BKTree()
        self._pixel_cache: OrderedDict[str, np.ndarray] = OrderedDict()

    def _check_cancel(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CancelledProcessing("Job cancelled during deduplication")

    def _cache_put(self, frame_id: str, pixels: np.ndarray) -> None:
        self._pixel_cache.pop(frame_id, None)
        self._pixel_cache[frame_id] = pixels
        while len(self._pixel_cache) > self._PIXEL_CACHE_SIZE:
            self._pixel_cache.popitem(last=False)

    def _pixels_for(self, frame: FrameMetadata, signature: _StoredSignature) -> np.ndarray:
        self._check_cancel()
        cached = self._pixel_cache.pop(frame.id, None)
        if cached is not None:
            self._pixel_cache[frame.id] = cached
            return cached
        # ``signature.path`` already contains the root-dir join performed when
        # the representative was indexed; joining it a second time would be
        # wrong when callers pass a relative root directory.
        with Image.open(signature.path) as image:
            rgb = image.convert("RGB")
            rgb.thumbnail((384, 384), Image.Resampling.LANCZOS)
            loaded = np.asarray(rgb).copy()
        self._cache_put(frame.id, loaded)
        return loaded

    def _store_representative(
        self,
        frame: FrameMetadata,
        signature: ImageSignature,
    ) -> None:
        if signature.pixels is None or signature.path is None:
            raise ValueError("representative signatures require comparison pixels and a path")
        stored = _StoredSignature(
            signature.exact_hash,
            signature.perceptual_hash,
            signature.aspect_ratio,
            signature.width,
            signature.height,
            signature.path,
        )
        self._records[frame.id] = stored
        self._frames_by_id[frame.id] = frame
        self._order[frame.id] = len(self.representatives)
        self._cache_put(frame.id, signature.pixels)
        self._phash_index.add(signature.perceptual_hash, frame.id)
        inspection_signature = ImageSignature(
            signature.exact_hash,
            signature.perceptual_hash,
            None,
            signature.aspect_ratio,
            signature.width,
            signature.height,
            signature.path,
        )
        self.representatives.append((frame, inspection_signature))

    def _native_change_guard(self, left: ImageSignature, right: _StoredSignature) -> bool:
        """Protect changes lost during thumbnail downsampling.

        Only candidates passing SSIM reach this stage.  Compare small native
        resolution tiles, never storing native arrays in the LRU.  Four
        high-contrast pixels are enough to preserve a tiny exponent or minus
        sign, even on a 4K slide.  This intentionally prefers a false suspect
        over discarding a meaningful annotation on a noisy capture.
        """

        if left.path is None:
            return True
        if (left.width, left.height) != (right.width, right.height):
            # Resolution changes are common after a re-encode.  The resized
            # SSIM/local gates already compared those frames in a common
            # thumbnail space; avoid retaining another full-resolution pair
            # just to repeat that work here.
            return False
        with Image.open(left.path) as first, Image.open(right.path) as second:
            area = left.width * left.height
            for y in range(0, left.height, 256):
                for x in range(0, left.width, 256):
                    self._check_cancel()
                    box = (x, y, min(x + 256, left.width), min(y + 256, left.height))
                    a = np.asarray(first.crop(box).convert("RGB"), dtype=np.int16)
                    b = np.asarray(second.crop(box).convert("RGB"), dtype=np.int16)
                    difference = np.max(np.abs(a - b), axis=2)
                    strong = difference >= 64
                    if int(np.count_nonzero(strong)) >= 4:
                        ys, xs = np.nonzero(strong)
                        tile_area = strong.size
                        bbox_fraction = float(
                            (xs.max() - xs.min() + 1) * (ys.max() - ys.min() + 1) / tile_area
                        )
                        if bbox_fraction <= 0.20:
                            return True
        return False

    def _near_match(
        self,
        frame: FrameMetadata,
        signature: ImageSignature,
    ) -> tuple[FrameMetadata, float, int] | None:
        candidates = self._phash_index.search(
            signature.perceptual_hash,
            self.thresholds.phash_distance,
        )
        candidates.sort(key=lambda item: (item[0], self._order.get(item[1], 0)))
        for distance, representative_id in candidates:
            self._check_cancel()
            representative = self._frames_by_id.get(representative_id)
            stored = self._records.get(representative_id)
            if representative is None or stored is None:
                continue
            if not stored.aspect_ratio or not signature.aspect_ratio:
                continue
            if abs(signature.aspect_ratio / stored.aspect_ratio - 1) > 0.015:
                continue
            if ocr_texts_conflict(frame.ocr_text, representative.ocr_text):
                # OCR is an aid only: it protects a candidate, while the
                # visual gates below remain the actual deduplication criteria.
                continue
            left = signature.pixels
            if left is None:
                continue
            try:
                right = self._pixels_for(representative, stored)
            except (OSError, ValueError):
                continue
            if _local_change_guard(left, right, self.thresholds.changed_fraction):
                continue
            similarity = _ssim(left, right)
            ssim_threshold = self.thresholds.ssim
            if (signature.width, signature.height) != (stored.width, stored.height):
                # Resampling and JPEG ringing lower SSIM without changing the
                # slide.  Keep the local/high-contrast guard strict and relax
                # only this resolution-conversion penalty slightly.
                ssim_threshold = max(0.90, ssim_threshold - 0.02)
            if similarity < ssim_threshold:
                continue
            try:
                if self._native_change_guard(signature, stored):
                    continue
            except (OSError, ValueError):
                continue
            return representative, similarity, distance
        return None

    def add(self, source_frame: FrameMetadata) -> FrameMetadata:
        self._check_cancel()
        frame = source_frame.model_copy(deep=True)
        frame.retained = bool(source_frame.retained) if source_frame.retention_overridden else True
        frame.duplicate_of = frame.matched_frame_id = frame.duplicate_reason = None
        frame.duplicate_similarity = None
        try:
            signature = image_signature(frame.path, self.root_dir)
        except (OSError, ValueError):
            # Keep a frame with a clear path even if an external cleanup or a
            # corrupt source prevents hashing it.
            return frame
        frame.exact_hash, frame.perceptual_hash = signature.exact_hash, signature.perceptual_hash

        if self.mode == "off":
            return frame

        matched = self.exact.get(signature.exact_hash)
        similarity = 1.0 if matched is not None else None
        reason = "exact_hash" if matched is not None else None
        if matched is None:
            near = self._near_match(frame, signature)
            if near is not None:
                matched, similarity, distance = near
                reason = "ssim" if distance == 0 else "perceptual_hash"

        if matched is not None:
            frame.duplicate_of = frame.matched_frame_id = matched.id
            frame.duplicate_reason = reason  # type: ignore[assignment]
            frame.duplicate_similarity = round(float(similarity or 0.0), 6)
            # Manual review flags every suspect but never discards it.  An
            # explicit user retention decision wins in all modes.
            if not frame.retention_overridden:
                frame.retained = self.mode == "manual"
            self.exact[signature.exact_hash] = matched
            return frame

        # A user-discarded frame must not become the only representative of a
        # later frame that would otherwise be retained for export.
        if frame.retained:
            self.exact[signature.exact_hash] = frame
            self._store_representative(frame, signature)
        return frame


def deduplicate_frames(
    frames: Iterable[FrameMetadata],
    mode: DeduplicationMode = "balanced",
    root_dir: Path | str | None = None,
    cancel_event: Event | None = None,
    phash_distance: int | None = None,
) -> list[FrameMetadata]:
    deduplicator = FrameDeduplicator(
        mode,
        root_dir,
        cancel_event,
        phash_distance,
    )
    return [deduplicator.add(frame) for frame in frames]


dedupe_frames = deduplicate_frames
compute_image_signature = image_signature
