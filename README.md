# FrameNotes AI

FrameNotes AI turns permitted YouTube videos, playlists, or authorized local
video files into reviewable visual notes. It samples frames incrementally,
removes repeated visuals with a conservative layered matcher, lets a person
restore or discard frames, and generates searchable PDFs and editable-text
PowerPoint presentations.

This repository contains a local-first MVP. It does **not** bypass YouTube
access controls, DRM, authentication, age gates, or copyright restrictions.
YouTube availability is dependent on the source being publicly accessible to
the installed `yt-dlp` extractor and on the operator's permissions.

## What works

- Newline-separated YouTube watch, `youtu.be`, Shorts, embed, live-page, and
  playlist links (HTTPS only, canonical 11-character video IDs).
- Multiple authorized local paths and common image/video formats for offline
  work and tests. Local files must be below `FRAMENOTES_AUTHORIZED_ROOTS`.
- Best-effort preflight metadata analysis with title, duration, channel,
  thumbnail, playlist count, and per-entry errors where available.
- Browser local-upload fallback that streams a permitted image/video into an
  isolated job directory without exposing the server's filesystem path.
- Incremental OpenCV/FFprobe extraction with a 1–60 second interval, start/end
  window, per-video candidate limit, optional first/last frame, scene-change
  threshold, bounded frame dimensions, and cancellation.
- Exact decoded-pixel SHA-256, indexed pHash, SSIM, local-change protection,
  and optional normalized Tesseract OCR assistance. Semantic embeddings are
  deliberately not enabled in the MVP, so the app does not claim semantic
  understanding it did not perform.
- Strict, balanced, aggressive, manual-review, and off deduplication modes.
  Manual mode flags suspects but keeps them until the reviewer decides.
- Live SSE progress, source-level failures with partial playlist success,
  frame search/filtering, bulk retention changes, manual order metadata,
  thumbnail previews, cancellation, and isolated job cleanup.
- PDF layouts (single, grid, filmstrip, two, four, contact, notes), cover and
  contents pages, Unicode text where installed fonts permit, timestamps,
  source sections, OCR notes, page numbers, and validation.
- PowerPoint output with one contained frame per primary slide, optional cover
  and source section slides, editable captions/source labels/OCR text, 16:9 or
  4:3 output, and slide-count/file-size metadata.

## Architecture

```text
frontend/                 Next.js App Router + TypeScript + Tailwind
  app/page.tsx            Source → configure → process → review → results
  lib/api.ts              Typed REST/SSE client
  components/             Accessible UI primitives and frame cards

backend/app/
  main.py                 FastAPI routes, SSE, safe file delivery
  models.py               Pydantic request/persistence/response models
  urls.py                 Strict source parsing and playlist discovery
  media.py                Authorization, yt-dlp, FFprobe/OpenCV extraction
  processing.py           Bounded, cancellable pipeline orchestration
  dedupe.py               Exact/pHash/BK-tree/SSIM/change guards
  ocr.py                  Optional local Tesseract adapter
  exports.py              Validated PDF/PPTX generation
  worker.py               Local async task manager + bounded concurrency
  store.py                Atomic per-job JSON/filesystem persistence
```

The MVP intentionally does not require PostgreSQL, Redis, Celery, S3, or a
paid API. The file-backed store and in-process worker are portable for local
use. For a multi-instance deployment, replace `JobStore` with PostgreSQL and
`JobManager` with a durable queue/object-store worker while keeping the module
interfaces intact. An optional production-oriented relational schema is kept
in `backend/migrations/001_initial.sql`; it is not applied by the local MVP.

## Local setup

Requirements:

- Python 3.11+ and a working C compiler/runtime for the pinned binary wheels.
- Node.js 20+ and npm.
- FFmpeg/FFprobe for robust video codecs. OpenCV handles common codecs without
  FFmpeg, but FFmpeg is recommended.
- Tesseract is optional and only used when OCR is checked in the UI.

```bash
cd backend
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit FRAMENOTES_AUTHORIZED_ROOTS to the folder containing permitted local media.
uvicorn app.main:app --reload --port 8000
```

In another terminal:

```bash
cd frontend
npm ci
cp .env.example .env.local
npm run dev
```

Open <http://localhost:3000>. The interactive backend API is at
<http://localhost:8000/docs>.

For local files, the default example config authorizes `./media` relative to
the backend process. Put permitted media in `media/`, or set an absolute
authorized root. Do not set the root to `/`, a home directory, or a directory
containing secrets.

## Docker Compose

Docker is the easiest way to get FFmpeg and Tesseract consistently:

```bash
mkdir -p media backend/data
docker compose up --build
```

The web app is served at <http://localhost:3000>, FastAPI at
<http://localhost:8000>, and `media/` is mounted read-only into the backend.
The compose setup persists job files in `backend/data/`; it does not run while
the containers are stopped and is intended for local development, not a
multi-tenant production deployment.

## API quickstart

Create a local-file job:

```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H 'content-type: application/json' \
  -d '{
    "sources": "/workspace-or-local-path/lecture.mp4",
    "interval": 5,
    "max_frames": 500,
    "dedupe_mode": "balanced",
    "ocr_enabled": false
  }'
```

The local-path example must be a path visible to the backend and under an
authorized root. Poll the returned `id`, or connect to its SSE stream:

```bash
curl -N http://localhost:8000/api/v1/jobs/JOB_ID/progress
curl http://localhost:8000/api/v1/jobs/JOB_ID/frames?status=retained
curl http://localhost:8000/api/v1/jobs/JOB_ID/duplicates
```

Useful endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| POST | `/api/v1/analyze` | Real best-effort metadata preflight |
| POST | `/api/v1/jobs` | Queue a processing job |
| GET | `/api/v1/jobs/{id}` | Full persisted job state |
| GET | `/api/v1/jobs/{id}/progress` | SSE progress stream |
| POST | `/api/v1/jobs/{id}/cancel` | Request cancellation |
| GET | `/api/v1/jobs/{id}/frames` | Search/filter/paginate frames |
| GET | `/api/v1/jobs/{id}/duplicates` | Suspected duplicate frames |
| PATCH | `/api/v1/jobs/{id}/frames/{frame}` | Change retention |
| POST | `/api/v1/jobs/{id}/frames/bulk` | Change many retentions |
| PATCH | `/api/v1/jobs/{id}/frames/order` | Persist manual review order |
| POST | `/api/v1/jobs/{id}/export` | Queue PDF/PPTX rendering (returns `202`) |
| GET | `/api/v1/jobs/{id}/downloads` | Artifact metadata and URLs |
| DELETE | `/api/v1/jobs/{id}` | Cancel/delete job and files |

Example export:

```bash
curl -X POST http://localhost:8000/api/v1/jobs/JOB_ID/export \
  -H 'content-type: application/json' \
  -d '{
    "formats": ["pdf", "pptx"],
    "layout": "notes",
    "include_ocr_text": true,
    "include_cover": true,
    "include_sections": true,
    "title": "Distributed Systems Notes"
  }'
```

Export rendering is asynchronous. Follow the same job's SSE stream (or poll
`GET /api/v1/jobs/{id}`) until `stage` is `exported`, `export failed`, or
`export cancelled`, then read `/downloads`. A failed export leaves the frame
review intact so the user can correct settings and retry.

Browser clients call `GET /api/v1/session` first. The backend uses a signed,
HttpOnly, SameSite cookie to keep local jobs private to that browser session;
the cookie is never returned as an API token.

## Configuration and limits

All backend settings use the `FRAMENOTES_` prefix. Important defaults are:

| Setting | Default | Why it exists |
| --- | ---: | --- |
| `AUTHORIZED_ROOTS` | empty | Local media is opt-in |
| `DEFAULT_INTERVAL` | `5` | Candidate sampling interval |
| `PLAYLIST_MAX_ITEMS` | `30` | Prevent unexpectedly huge playlist jobs |
| `MAX_TOTAL_FRAMES` | `6000` | Combined job CPU/disk bound |
| `MAX_VIDEO_SECONDS` | `14400` | Four-hour per-video bound |
| `MAX_SOURCE_BYTES` | `1 GiB` | Download/input bound |
| `MAX_JOB_BYTES` | `2 GiB` | Isolated job directory bound |
| `MAX_JOB_SECONDS` | `3600` | Worker wall-clock bound |
| `MAX_CONCURRENT_JOBS` | `2` | Local CPU/network bound |
| `MAX_QUEUED_JOBS` | `8` | Back-pressure before work begins |
| `RETENTION_HOURS` | `24` | Expiration metadata for cleanup policy |

The current local MVP stores expiration metadata and removes downloaded media
after processing. A production cleanup scheduler should delete expired job
directories using the same `expires_at` policy. No credentials or cookies are
accepted by the YouTube downloader.

## Deduplication behavior

The matcher keeps a stable first representative in source order:

1. Hash decoded RGB pixels with SHA-256 for exact visual repeats.
2. Query a bounded BK-tree of pHash values rather than comparing every pair.
3. Compare aspect ratio, local/high-contrast changes, and SSIM.
4. Protect OCR text/digit changes when OCR is enabled. OCR never replaces the
   visual gates and no semantic embedding claim is made.

`strict` uses the narrowest near-match thresholds, `balanced` is the default,
`aggressive` removes more suspects, `manual` annotates but retains suspects,
and `off` keeps every sampled candidate. The combined job index covers frames
in playlist/input order, so repeated visual introductions in later sources
are visible as duplicates with `matched_frame_id`; source identity is never
lost.

The matcher deliberately favors retention when local changes suggest a new
equation, number, diagram, or progressive bullet reveal. It is not a promise
of human-level semantic understanding.

## Privacy and security boundaries

- Only HTTPS YouTube hosts are accepted for network work. Generic CDN URLs,
  protocol-relative URLs, credentials, non-standard ports, HTTP, arbitrary
  redirects, and non-YouTube extractors are rejected.
- Local paths are canonicalized before containment checks, including symlink
  resolution, and must be beneath an explicit authorized root.
- Job state, frames, and exports are isolated below one generated job ID.
  Download and image endpoints validate filenames and resolved paths.
- FFmpeg, FFprobe, yt-dlp, and Tesseract are invoked with argument arrays,
  never shell fragments. Child process groups are terminated on cancellation.
- Duration, file-size, frame-count, queue, concurrency, and wall-clock caps
  are enforced. Remote retries are restricted to recognizable transient
  transport errors; private/deleted/DRM/auth failures are not retried.
- This MVP has no user authentication. Do not expose it directly to an
  untrusted network. Put it behind authentication, rate limiting, per-user
  job authorization, and an object store with signed URLs before deployment.

## Testing and verification

Backend tests generate synthetic PNGs and OpenCV videos; they do not contact
YouTube or depend on paid services:

```bash
cd backend
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q app tests
```

Frontend checks:

```bash
cd frontend
npm ci
npm run typecheck
npm run build
```

The test suite covers URL canonicalization and rejection, playlist order and
duplicate entry handling, exact/near/manual deduplication, timestamped frame
extraction, source isolation and partial failure behavior, cancellation,
manual retention, safe image delivery, PDF/PPTX validity, and generated
artifact metadata. The browser UI is connected to the real API; it does not
use fabricated frame or progress data.

## Troubleshooting

### “Local source is outside authorized folders”

Set `FRAMENOTES_AUTHORIZED_ROOTS` to the absolute folder containing the
permitted file, restart FastAPI, and submit the same path. In Docker, use a
path inside the mounted `/workspace/media` folder, not the host path.

### YouTube metadata or download fails

Run `yt-dlp --version`, update the pinned dependency when the YouTube
extractor changes, and confirm that the video is public and permitted for the
operator. FrameNotes intentionally reports private, deleted, age-restricted,
DRM-protected, live, and authentication-required sources rather than trying
to bypass those restrictions.

### Frames are missing or timestamps look wrong

Install FFmpeg/FFprobe and retry with a supported local codec. The extractor
uses FFprobe PTS when available and a real decoder FPS otherwise; it refuses
to invent timing metadata when neither is available. A smaller interval may
increase candidate count, subject to the 1–60 second and frame-cap limits.

### OCR is empty

OCR is optional. Install `tesseract-ocr`, enable OCR in the configuration
screen, and check the job's `ocr_status`. If the binary is unavailable, the
job completes with a clear no-op status instead of failing the export.

### Docker build has a stale frontend API URL

`NEXT_PUBLIC_API_BASE_URL` is baked into the Next.js browser bundle at build
time. Pass the correct value as the Compose build argument and rebuild the
frontend; the browser must be able to reach that URL directly.

## Production follow-up

The MVP's clear seams make the next phase straightforward:

1. Add authenticated users and job ownership checks to every route.
2. Move job metadata to PostgreSQL and background work to Redis/Celery/RQ.
3. Store frames/exports in S3-compatible storage and issue signed URLs.
4. Add a durable cleanup worker for `expires_at` and quota accounting.
5. Add metrics for queue wait, decode time, disk usage, OCR duration, and
   export failures.
6. Add optional embedding analysis behind a feature flag and keep it
   conservative, explainable, and reviewable.
