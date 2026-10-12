# FrameNotes AI backend

This directory contains a runnable local MVP for extracting presentation
frames from authorized local files and permitted public YouTube sources,
removing repeated slides, and exporting the retained frames to PDF and
PowerPoint. Generic remote URLs are intentionally rejected at the source
boundary.

## Quick start

```bash
cd backend
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

The API is available at `http://127.0.0.1:8000`, with interactive docs at
`/docs`.  `ffmpeg` is recommended for codecs that OpenCV cannot decode and
`yt-dlp` is used for YouTube/remote downloads.  OCR is optional: pass
`ocr_enabled: true` and the worker uses a local Tesseract executable when it
is installed; otherwise OCR is a no-op.

`FRAMENOTES_AUTHORIZED_ROOTS` is a path-separator, comma-separated list, or
JSON list of folders from which local media may be read. It defaults to empty;
local access is opt-in. Job directories are isolated under
`FRAMENOTES_JOBS_DIR` and contain `job.json`, `frames/`, `work/`, and
`downloads/`. Downloaded source media in `work/` is removed after a job reaches
a terminal state. Duration, source/job byte, frame, queue, concurrency, and
wall-clock caps are configured in `.env.example`.

## API flow

Create a job with a local video, a YouTube URL, or multi-line input. Local paths
must be below an authorized root; only permitted public YouTube sources are
downloaded through yt-dlp when the source is processed:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs \
  -H 'content-type: application/json' \
  -d '{"sources":"/authorized/talk.mp4\nhttps://youtu.be/dQw4w9WgXcQ", \
       "interval":5,"start_time":0,"max_frames":100,
        "dedupe_mode":"balanced","ocr_enabled":false}'
```

Use `POST /api/v1/analyze` first for best-effort real metadata discovery. The
response contains the job `id`. Poll `GET /api/v1/jobs/{id}`, stream
`GET /api/v1/jobs/{id}/progress` as Server-Sent Events, inspect
`GET /api/v1/jobs/{id}/frames` and `GET /api/v1/jobs/{id}/duplicates`, then
optionally change `retained` with
`PATCH /api/v1/jobs/{id}/frames/{frame_id}/retention`.

Export after processing:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/jobs/JOB_ID/export \
  -H 'content-type: application/json' \
  -d '{"formats":["pdf","pptx"],"layout":"grid","columns":2}'
curl http://127.0.0.1:8000/api/v1/jobs/JOB_ID/downloads
```

Export requests return `202` with `status: "exporting"`; poll the job or
follow its SSE stream until `stage` is `exported`, `export failed`, or
`export cancelled`, then fetch downloads. Extraction frames remain available
when an export fails.

Downloads are available at
`/api/v1/jobs/{job_id}/downloads/{filename}` (and the equivalent export route
`/api/v1/jobs/{job_id}/exports/{filename}` or flat
`/api/v1/downloads/{job_id}/{filename}`). `GET /api/v1/jobs/{id}/frames` also
returns a safe `image_url` for each extracted frame. `DELETE /api/v1/jobs/{id}`
cancels active work, waits for the cancellable worker, and removes the
isolated job directory.

## Processing details

* HTTPS YouTube watch, `youtu.be`, shorts, embed, and playlist links are
  canonicalised and deduplicated. Generic network URLs, credentials, HTTP,
  and non-YouTube extractors are rejected. Playlists are expanded through
  yt-dlp's flat metadata mode while preserving unavailable entries as errors.
* Frame extraction is sequential and timestamp ordered, using the configured
  interval/start/end/max-frame limits. OpenCV handles common local codecs;
  ffmpeg is used as a cancellable normalization fallback when OpenCV cannot
  open a local file. Local images are also accepted as a useful one-frame
  input for tests and small workflows.
* Deduplication keeps the first representative in combined source order.
  `strict`, `balanced`, and `aggressive` modes combine exact pixel SHA-256,
  indexed perceptual hash, SSIM, local-change protection, and optional OCR
  guards. `manual` flags exact/near suspects but retains them for confirmation.
* PDF and PPTX exports include an optional cover, source sections, timestamps,
  and aspect-ratio-preserving image placement. `grid`, `single`, and
  `filmstrip` layouts are supported. Generated files are signature checked
  before being listed as downloads.

## API notes

`POST /api/v1/jobs` accepts `sources` as either a list or a newline-delimited
string. Identical YouTube watch/short/youtu.be links are canonicalized before
playlist expansion. A malformed URL is rejected synchronously with HTTP 422;
an inaccessible local/remote source produces a terminal failed job with the
source error persisted in `GET /api/v1/jobs/{id}`.

`GET /api/v1/jobs/{id}/progress` is an SSE stream. It emits JSON `progress`
events and closes after `completed`, `failed`, or `cancelled`. Frame listings
support `status=retained|discarded`, `search`, `offset`/`limit`, and a numeric
`cursor`; duplicate listings expose `duplicate_of`, `matched_frame_id`,
`duplicate_reason`, and `duplicate_similarity`.

OCR remains disabled by default. If requested, the worker uses the local
`tesseract` executable when available and otherwise records a no-op reason
without failing the job.

## Tests

From `backend/`:

```bash
python -m pytest
```

The suite creates synthetic PNG/video media, exercises URL parsing,
deduplication, cancellation/restore, API ordering and retention, and opens
the generated PDF/PPTX packages for validity checks.  No network or paid
service is required by the tests.
