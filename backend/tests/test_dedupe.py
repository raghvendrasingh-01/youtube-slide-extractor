from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from app.dedupe import deduplicate_frames
from app.models import FrameMetadata


def _frame(path: Path, frame_id: str, index: int) -> FrameMetadata:
    return FrameMetadata(
        id=frame_id,
        source_id="source-0001",
        source_title="Synthetic",
        index=index,
        timestamp=float(index),
        path=path.name,
        width=path.stat().st_size,
        height=1,
    )


def test_exact_and_balanced_near_duplicate_metadata(tmp_path: Path) -> None:
    first = Image.new("RGB", (320, 180), "white")
    draw = ImageDraw.Draw(first)
    draw.rectangle((20, 20, 300, 160), fill="#2266aa")
    first.save(tmp_path / "one.png")
    first.save(tmp_path / "same.png")
    near = first.copy()
    ImageDraw.Draw(near).rectangle((22, 22, 298, 158), fill="#2266aa")
    ImageDraw.Draw(near).rectangle((145, 80, 150, 85), fill="#2a6db1")
    near.save(tmp_path / "near.png")

    frames = [
        _frame(tmp_path / "one.png", "f1", 0),
        _frame(tmp_path / "same.png", "f2", 1),
        _frame(tmp_path / "near.png", "f3", 2),
    ]
    result = deduplicate_frames(frames, mode="balanced", root_dir=tmp_path)
    assert result[0].retained is True
    assert result[1].duplicate_of == "f1"
    assert result[1].duplicate_reason == "exact_hash"
    assert result[2].duplicate_of == "f1"
    assert result[2].matched_frame_id == "f1"
    assert result[2].duplicate_reason in {"perceptual_hash", "ssim"}


def test_manual_mode_flags_near_duplicates_without_discarding_them(tmp_path: Path) -> None:
    image = Image.new("RGB", (80, 50), "#cc8844")
    image.save(tmp_path / "a.png")
    image.save(tmp_path / "b.png")
    changed = Image.new("RGB", (80, 50), "#cc8845")
    changed.save(tmp_path / "c.png")
    frames = [_frame(tmp_path / name, fid, index) for index, (name, fid) in enumerate(
        [("a.png", "a"), ("b.png", "b"), ("c.png", "c")]
    )]
    result = deduplicate_frames(frames, mode="manual", root_dir=tmp_path)
    # Manual review is intentionally conservative: suspected exact/near
    # duplicates remain available until the user makes the final decision.
    assert [item.retained for item in result] == [True, True, True]
    assert result[1].duplicate_of == "a"
