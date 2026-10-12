from __future__ import annotations

import time
from pathlib import Path

import pytest
from PIL import Image

cv2 = pytest.importorskip("cv2")
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.main import create_app
from app.media import ExtractionConfig, extract_frames
from app.store import JobStore
from app.models import JobCreateRequest
from app.processing import process_job
from threading import Event


def _video(path: Path, frame_count: int = 8, fps: float = 4.0) -> Path:
    writer = cv2.VideoWriter(
        str(path),
        cv2.VideoWriter_fourcc(*"mp4v"),
        fps,
        (160, 90),
    )
    if not writer.isOpened():
        pytest.skip("OpenCV mp4v writer is unavailable")
    for index in range(frame_count):
        image = cv2.cvtColor(
            __import__("numpy").full((90, 160, 3), (index * 20, 80, 180), dtype="uint8"),
            cv2.COLOR_RGB2BGR,
        )
        writer.write(image)
    writer.release()
    return path


def test_extraction_respects_interval_and_timestamp_order(tmp_path: Path) -> None:
    source = _video(tmp_path / "synthetic.mp4", frame_count=12, fps=4)
    output = tmp_path / "out"
    frames = extract_frames(source, output, ExtractionConfig(interval=0.5, max_frames=10))
    assert frames
    assert [frame.timestamp for frame in frames] == sorted(frame.timestamp for frame in frames)
    assert len(frames) <= 10
    assert frames[0].timestamp == 0
    assert frames[1].timestamp >= 0.49


def test_extraction_end_time_is_exclusive(tmp_path: Path) -> None:
    source = _video(tmp_path / "window.mp4", frame_count=8, fps=4)
    frames = extract_frames(source, tmp_path / "window-out", ExtractionConfig(interval=0.25, end_time=0.5))
    assert frames
    assert all(frame.timestamp < 0.5 for frame in frames)


def test_api_job_frames_retention_export_and_delete(tmp_path: Path) -> None:
    source = _video(tmp_path / "api.mp4", frame_count=4, fps=2)
    settings = AppSettings(jobs_dir=tmp_path / "jobs", authorized_roots=[tmp_path], default_interval=1)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={"sources": str(source), "interval": 1, "max_frames": 10, "dedupe_mode": "manual"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job
        frames = client.get(f"/api/v1/jobs/{job_id}/frames?limit=50").json()
        assert frames["total"] >= 1
        assert [item["timestamp"] for item in frames["frames"]] == sorted(
            item["timestamp"] for item in frames["frames"]
        )
        frame_id = frames["frames"][0]["id"]
        retained = client.patch(
            f"/api/v1/jobs/{job_id}/frames/{frame_id}/retention",
            json={"retained": False},
        )
        assert retained.status_code == 200
        assert retained.json()["frames"][0]["retained"] is False
        export = client.post(f"/api/v1/jobs/{job_id}/export", json={"format": "pdf"})
        assert export.status_code == 202, export.text
        assert export.json()["status"] == "exporting"
        for _ in range(100):
            exported = client.get(f"/api/v1/jobs/{job_id}").json()
            if exported["stage"] in {"exported", "export failed", "export cancelled"}:
                break
            time.sleep(0.02)
        assert exported["stage"] == "exported", exported
        downloads = client.get(f"/api/v1/jobs/{job_id}/downloads").json()["downloads"]
        assert downloads
        file_response = client.get(f"/api/v1/jobs/{job_id}/downloads/{downloads[0]['filename']}")
        assert file_response.status_code == 200
        assert file_response.content.startswith(b"%PDF-")
        assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 204
        assert client.get(f"/api/v1/jobs/{job_id}").status_code == 404


def test_api_accepts_browser_aliases_and_exposes_duplicate_images(tmp_path: Path) -> None:
    first = Image.new("RGB", (120, 80), "#2a5b88")
    one = tmp_path / "one.png"
    two = tmp_path / "two.png"
    first.save(one)
    first.save(two)
    settings = AppSettings(jobs_dir=tmp_path / "jobs", authorized_roots=[tmp_path], worker_poll_seconds=0.05)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs",
            json={
                "urls": [str(one), str(two)],
                "config": {"intervalSeconds": 1, "maxFrames": 10, "dedupeMode": "exact", "ocrEnabled": False},
            },
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed", job
        duplicates = client.get(f"/api/v1/jobs/{job_id}/duplicates").json()
        assert duplicates["total"] == 1
        duplicate = duplicates["frames"][0]
        assert duplicate["duplicate_reason"] == "exact_hash"
        image = client.get(duplicate["image_url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/")


def test_per_video_frame_cap_does_not_skip_later_sources(tmp_path: Path) -> None:
    first = _video(tmp_path / "first.mp4", frame_count=4, fps=2)
    second = _video(tmp_path / "second.mp4", frame_count=4, fps=2)
    settings = AppSettings(jobs_dir=tmp_path / "jobs", authorized_roots=[tmp_path], worker_poll_seconds=0.05)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            json={"sources": [str(first), str(second)], "interval": 1, "max_frames": 2, "dedupe_mode": "manual"},
        )
        job_id = response.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        assert job["sources"][0]["status"] == "completed"
        assert job["sources"][1]["status"] == "completed"
        assert all(source["frame_count"] <= 2 for source in job["sources"])


def test_restore_marks_interrupted_job_failed(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs")
    record = store.create(JobCreateRequest(sources=["/tmp/video.mp4"]))
    store.update(record.id, status="running", stage="extracting")
    restored = JobStore(tmp_path / "jobs").restore()
    assert restored[0].status == "failed"
    assert restored[0].stage == "interrupted"


def test_cancellation_before_processing_is_persisted(tmp_path: Path) -> None:
    source = Image.new("RGB", (80, 50), "#225588")
    source_path = tmp_path / "one.png"
    source.save(source_path)
    store = JobStore(tmp_path / "jobs")
    record = store.create(JobCreateRequest(sources=[str(source_path)], interval=1))
    event = Event()
    event.set()
    final = process_job(record.id, store, AppSettings(jobs_dir=tmp_path / "jobs", authorized_roots=[tmp_path]), event)
    assert final.status == "cancelled"


def test_analyze_thumbnail_and_manual_order_endpoints(tmp_path: Path) -> None:
    image = Image.new("RGB", (120, 80), "#225588")
    source = tmp_path / "slide.png"
    image.save(source)
    settings = AppSettings(jobs_dir=tmp_path / "jobs", authorized_roots=[tmp_path], worker_poll_seconds=0.02)
    app = create_app(settings)
    with TestClient(app) as client:
        analysis = client.post("/api/v1/analyze", json={"sources": str(source)})
        assert analysis.status_code == 200, analysis.text
        assert analysis.json()["sources"][0]["duration"] == 0

        created = client.post(
            "/api/v1/jobs",
            json={"sources": str(source), "interval": 1, "max_frames": 5, "dedupe_mode": "manual"},
        )
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        for _ in range(100):
            job = client.get(f"/api/v1/jobs/{job_id}").json()
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "completed"
        frame_id = job["frames"][0]["id"]
        assert client.get(f"/api/v1/jobs/{job_id}/frames/{frame_id}/thumbnail").status_code == 200
        reordered = client.patch(
            f"/api/v1/jobs/{job_id}/frames/order",
            json={"frame_ids": [frame_id]},
        )
        assert reordered.status_code == 200, reordered.text
        assert reordered.json()["frames"][0]["review_order"] == 0


def test_session_ownership_and_raw_upload_flow(tmp_path: Path) -> None:
    image = Image.new("RGB", (96, 64), "#345678")
    upload_path = tmp_path / "fixture.png"
    image.save(upload_path)
    settings = AppSettings(
        jobs_dir=tmp_path / "jobs",
        authorized_roots=[],
        max_source_bytes=2_000_000,
        worker_poll_seconds=0.02,
    )
    app = create_app(settings)
    with TestClient(app) as owner, TestClient(app) as other:
        assert other.get("/api/v1/session").status_code == 200
        config = {
            "sources": ["upload"],
            "interval": 1,
            "max_frames": 5,
            "dedupe_mode": "balanced",
            "ocr_enabled": False,
        }
        response = owner.post(
            "/api/v1/jobs/upload",
            params={"filename": "lecture.png", "config": __import__("json").dumps(config)},
            content=upload_path.read_bytes(),
            headers={"content-type": "image/png"},
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert response.json()["request"]["sources"] == ["upload.png"]
        for _ in range(100):
            record = owner.get(f"/api/v1/jobs/{job_id}").json()
            if record["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        assert record["status"] == "completed", record
        assert other.get(f"/api/v1/jobs/{job_id}").status_code == 404
        assert other.get("/api/v1/jobs").status_code == 200
        assert other.get("/api/v1/jobs").json() == []
