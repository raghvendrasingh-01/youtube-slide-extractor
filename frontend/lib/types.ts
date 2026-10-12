export type JobStatus =
  | "queued"
  | "running"
  | "cancelling"
  | "exporting"
  | "completed"
  | "cancelled"
  | "failed";
export type DedupeMode =
  "strict" | "balanced" | "aggressive" | "manual" | "off";
export type ExportFormat = "pdf" | "pptx";
export type ExportLayout =
  "grid" | "single" | "filmstrip" | "two" | "four" | "contact" | "notes";

export interface JobRequest {
  sources: string[];
  interval: number;
  start_time: number;
  end_time?: number | null;
  max_frames: number;
  dedupe_mode: DedupeMode;
  ocr_enabled: boolean;
  phash_distance: number;
  include_first: boolean;
  include_last: boolean;
  scene_threshold: number;
  title?: string | null;
}

export interface AnalyzedSource {
  input: string;
  canonical: string;
  kind: "local" | "youtube_video" | "youtube_playlist" | "remote" | "invalid";
  title: string;
  video_id?: string | null;
  duration?: number | null;
  channel?: string | null;
  thumbnail?: string | null;
  playlist_count?: number | null;
  error?: string | null;
}

export interface AnalyzeResponse {
  sources: AnalyzedSource[];
  count: number;
}

export interface SourceRecord {
  id: string;
  input: string;
  canonical: string;
  kind: "local" | "youtube_video" | "youtube_playlist" | "remote";
  title: string;
  status:
    "queued" | "running" | "completed" | "failed" | "cancelled" | "skipped";
  error?: string | null;
  frame_count: number;
  video_id?: string | null;
  duration?: number | null;
  thumbnail?: string | null;
  channel?: string | null;
  playlist_count?: number | null;
  position?: number;
  exact_removed?: number;
  near_removed?: number;
  retained_count?: number;
  processing_seconds?: number;
}

export interface FrameMetadata {
  id: string;
  source_id: string;
  source_title: string;
  index: number;
  timestamp: number;
  path: string;
  width: number;
  height: number;
  exact_hash?: string | null;
  perceptual_hash?: string | null;
  retained: boolean;
  duplicate_of?: string | null;
  matched_frame_id?: string | null;
  duplicate_reason?: "exact_hash" | "perceptual_hash" | "ssim" | null;
  duplicate_similarity?: number | null;
  ocr_text?: string | null;
  image_url?: string | null;
  thumbnail_url?: string | null;
  retention_overridden?: boolean;
  review_order?: number | null;
}

export interface DownloadArtifact {
  filename: string;
  format: ExportFormat;
  size_bytes: number;
  created_at: string;
  download_url?: string | null;
  page_count?: number;
  source_id?: string | null;
}

export interface JobRecord {
  id: string;
  status: JobStatus;
  progress: number;
  stage: string;
  error?: string | null;
  created_at: string;
  updated_at: string;
  request: JobRequest;
  sources: SourceRecord[];
  frames: FrameMetadata[];
  downloads: DownloadArtifact[];
  ocr_status: string;
  frame_count: number;
  duplicate_count: number;
  retained_count: number;
  warnings?: string[];
  techniques?: string[];
  processing_seconds?: number;
  expires_at?: string | null;
}

export interface ProgressEvent {
  type: "progress" | "stage" | "complete" | "error" | "cancelled";
  job_id: string;
  status: JobStatus;
  progress: number;
  stage: string;
  frame_count: number;
  duplicate_count: number;
  retained_count: number;
  ocr_status: string;
  error?: string | null;
  current_video?: string | null;
  total_videos?: number;
  completed_videos?: number;
  warnings?: string[];
}

export interface FrameListResponse {
  frames: FrameMetadata[];
  total: number;
  offset: number;
  limit: number;
  next_cursor?: string | null;
}

export interface ExportRequest {
  images_only: boolean;
  formats: ExportFormat[];
  layout: ExportLayout;
  columns: number;
  include_cover: boolean;
  include_timestamps: boolean;
  include_source_titles: boolean;
  title: string;
  include_ocr_text?: boolean;
  include_sections?: boolean;
  separate_videos?: boolean;
  page_size?: "A4" | "Letter";
  orientation?: "portrait" | "landscape";
  margin_mm?: number;
  aspect_ratio?: "16:9" | "4:3";
  footer?: string;
}

export interface ExportResponse {
  job_id: string;
  status: "exporting" | "completed" | "failed";
  downloads: DownloadArtifact[];
}

export interface JobSummary {
  id: string;
  status: JobStatus;
  progress: number;
  stage: string;
  error?: string | null;
  frame_count: number;
  duplicate_count: number;
  retained_count: number;
  ocr_status: string;
  warnings?: string[];
  techniques?: string[];
  processing_seconds?: number;
  expires_at?: string | null;
  downloads: DownloadArtifact[];
}

export interface JobsResponse {
  jobs: JobSummary[];
}

export interface ExportSummary {
  videos: number;
  frames_extracted: number;
  frames_retained: number;
  frames_discarded: number;
  ocr_completed: number;
}

export interface ExportListResponse {
  job_id: string;
  exports: (DownloadArtifact & { id?: string })[];
  summary: ExportSummary;
}
