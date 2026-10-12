-- Optional PostgreSQL schema for the production persistence adapter.
-- The local MVP intentionally uses backend/app/store.py (atomic JSON files),
-- so this migration is not required for local startup.

CREATE TABLE IF NOT EXISTS processing_jobs (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    input_type TEXT NOT NULL,
    configuration JSONB NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    progress DOUBLE PRECISION NOT NULL DEFAULT 0,
    error TEXT,
    warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
    total_video_count INTEGER NOT NULL DEFAULT 0,
    completed_video_count INTEGER NOT NULL DEFAULT 0,
    total_candidate_frames INTEGER NOT NULL DEFAULT 0,
    total_retained_frames INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS processing_jobs_owner_created_idx
    ON processing_jobs (owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS video_sources (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES processing_jobs(id) ON DELETE CASCADE,
    video_id TEXT,
    input TEXT NOT NULL,
    canonical TEXT NOT NULL,
    title TEXT NOT NULL,
    channel TEXT,
    thumbnail TEXT,
    duration DOUBLE PRECISION,
    playlist_position INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    error TEXT,
    frame_count INTEGER NOT NULL DEFAULT 0,
    retained_count INTEGER NOT NULL DEFAULT 0,
    exact_removed INTEGER NOT NULL DEFAULT 0,
    near_removed INTEGER NOT NULL DEFAULT 0,
    processing_seconds DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS video_sources_job_position_idx
    ON video_sources (job_id, playlist_position);

CREATE TABLE IF NOT EXISTS extracted_frames (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES video_sources(id) ON DELETE CASCADE,
    timestamp_seconds DOUBLE PRECISION NOT NULL,
    source_order INTEGER NOT NULL,
    image_storage_key TEXT NOT NULL,
    thumbnail_storage_key TEXT,
    exact_hash TEXT,
    perceptual_hash TEXT,
    ocr_text TEXT,
    retained BOOLEAN NOT NULL DEFAULT true,
    retention_overridden BOOLEAN NOT NULL DEFAULT false,
    duplicate_reason TEXT,
    duplicate_similarity DOUBLE PRECISION,
    matched_frame_id TEXT REFERENCES extracted_frames(id),
    review_order INTEGER
);

CREATE INDEX IF NOT EXISTS extracted_frames_source_order_idx
    ON extracted_frames (source_id, review_order, timestamp_seconds);

CREATE TABLE IF NOT EXISTS export_artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES processing_jobs(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    storage_key TEXT NOT NULL,
    file_size_bytes BIGINT NOT NULL,
    page_or_slide_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
