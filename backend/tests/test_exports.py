from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.exports import create_exports
from app.models import ExportRequest, FrameMetadata, JobCreateRequest, JobRecord, SourceRecord


def _job(tmp_path: Path) -> JobRecord:
    frames_dir = tmp_path / "frames" / "source-0001"
    frames_dir.mkdir(parents=True)
    frame_path = frames_dir / "frame-000000.jpg"
    Image.new("RGB", (640, 360), "#335577").save(frame_path)
    return JobRecord(
        id="job-export",
        request=JobCreateRequest(sources=["demo.mp4"]),
        sources=[
            SourceRecord(
                id="source-0001",
                input="demo.mp4",
                canonical="/tmp/demo.mp4",
                kind="local",
                title="Synthetic lecture",
                status="completed",
                frame_count=1,
            )
        ],
        frames=[
            FrameMetadata(
                id="source-0001-frame-000000",
                source_id="source-0001",
                source_title="Synthetic lecture",
                index=0,
                timestamp=12.5,
                path="frames/source-0001/frame-000000.jpg",
                width=640,
                height=360,
            )
        ],
        status="completed",
        progress=100,
    )


def test_pdf_and_pptx_exports_are_openable(tmp_path: Path) -> None:
    pytest.importorskip("reportlab")
    pytest.importorskip("pptx")
    job = _job(tmp_path)
    artifacts = create_exports(
        job,
        tmp_path,
        ExportRequest(formats=["pdf", "pptx"], layout="single"),
    )
    assert {artifact.format for artifact in artifacts} == {"pdf", "pptx"}
    pdf = tmp_path / "downloads" / next(item.filename for item in artifacts if item.format == "pdf")
    pptx = tmp_path / "downloads" / next(item.filename for item in artifacts if item.format == "pptx")
    assert pdf.read_bytes().startswith(b"%PDF-")
    assert b"%%EOF" in pdf.read_bytes()[-1024:]
    from pptx import Presentation

    presentation = Presentation(str(pptx))
    assert len(presentation.slides) >= 2


def test_images_only_export_contains_only_frame_images(tmp_path: Path) -> None:
    pytest.importorskip("reportlab")
    job = _job(tmp_path)
    artifacts = create_exports(
        job,
        tmp_path,
        ExportRequest(formats=["pdf"], images_only=True),
    )
    pdf = tmp_path / "downloads" / artifacts[0].filename

    assert artifacts[0].page_count == 1
    assert b"/Subtype /Image" in pdf.read_bytes()

    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        result = subprocess.run(
            [pdftotext, str(pdf), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == ""
