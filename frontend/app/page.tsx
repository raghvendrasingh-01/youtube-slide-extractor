"use client";
/* eslint-disable @next/next/no-img-element */

import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ClipboardPaste,
  Clock3,
  Download,
  ExternalLink,
  FileImage,
  FileText,
  Film,
  Filter,
  FolderOpen,
  Gauge,
  HardDriveUpload,
  Layers3,
  Link2,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  StopCircle,
  Trash2,
  Upload,
  Video,
  WandSparkles,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  analyzeSources,
  bulkUpdateRetention,
  cancelJob,
  createExport,
  createJob,
  deleteJob,
  getAllFrames,
  getExports,
  getHealth,
  getJob,
  getJobs,
  initializeSession,
  frameImageUrl,
  downloadUrl,
  reorderFrames,
  subscribeToProgress,
  updateFrameRetention,
  uploadSource,
} from "@/lib/api";
import type {
  AnalyzedSource,
  DedupeMode,
  DownloadArtifact,
  ExportLayout,
  ExportRequest,
  FrameMetadata,
  JobRecord,
  JobRequest,
  JobStatus,
  JobSummary,
  ProgressEvent,
  SourceRecord,
} from "@/lib/types";
import { FrameCard } from "@/components/frame-card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/dialog";
import {
  Badge,
  BrandMark,
  HelpTip,
  LoadingLabel,
  ProgressBar,
  SelectControl,
  StepPill,
  ThemeToggle,
  cn,
} from "@/components/ui";

type Phase = "landing" | "config" | "processing" | "review" | "results";
type ProcessingMode = "extract" | "export";
type SourceMode = "links" | "upload";
type ReviewTab = "retained" | "suspected" | "discarded";

interface DraftConfig {
  interval: number;
  startTime: string;
  endTime: string;
  maxFrames: number;
  dedupeMode: DedupeMode;
  ocrEnabled: boolean;
  phashDistance: number;
  includeFirst: boolean;
  includeLast: boolean;
  sceneThreshold: string;
  formats: Array<"pdf" | "pptx">;
  imagesOnly: boolean;
  layout: ExportLayout;
  includeCover: boolean;
  includeTimestamps: boolean;
  includeSourceTitles: boolean;
  includeOcrText: boolean;
  separateVideos: boolean;
  includeSections: boolean;
  pageSize: "A4" | "Letter";
  orientation: "portrait" | "landscape";
  aspectRatio: "16:9" | "4:3";
  marginMm: number;
  footer: string;
  title: string;
  ownsContent: boolean;
  acceptsTerms: boolean;
}

const initialConfig: DraftConfig = {
  interval: 5,
  startTime: "0",
  endTime: "",
  maxFrames: 500,
  dedupeMode: "balanced",
  ocrEnabled: false,
  phashDistance: 6,
  includeFirst: true,
  includeLast: false,
  sceneThreshold: "0",
  formats: ["pdf", "pptx"],
  imagesOnly: true,
  layout: "single",
  includeCover: true,
  includeTimestamps: true,
  includeSourceTitles: true,
  includeOcrText: false,
  separateVideos: false,
  includeSections: true,
  pageSize: "A4",
  orientation: "portrait",
  aspectRatio: "16:9",
  marginMm: 12,
  footer: "FrameNotes AI",
  title: "FrameNotes visual notes",
  ownsContent: false,
  acceptsTerms: false,
};

const TERMINAL_STATUSES: JobStatus[] = ["completed", "cancelled", "failed"];

function parseSources(value: string) {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim().replace(/^[-*]\s+/, ""))
    .filter(Boolean)
    .filter((value, index, values) => values.indexOf(value) === index);
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function seconds(value: number) {
  if (!Number.isFinite(value)) return "—";
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function humanStatus(status: string) {
  return status
    .replace(/[-_]/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function statusTone(
  status: JobStatus,
): "neutral" | "success" | "warning" | "danger" {
  if (status === "failed") return "danger";
  if (status === "cancelled" || status === "cancelling") return "warning";
  if (status === "completed") return "success";
  return "neutral";
}

function isExportTerminal(record: JobRecord) {
  return (
    record.status === "failed" ||
    record.status === "cancelled" ||
    (record.status === "completed" &&
      (record.stage.toLowerCase() === "exported" ||
        record.stage.toLowerCase() === "export failed" ||
        record.stage.toLowerCase() === "export cancelled"))
  );
}

function isExtractionTerminal(record: JobRecord) {
  return (
    TERMINAL_STATUSES.includes(record.status) &&
    !record.stage.toLowerCase().includes("export")
  );
}

function sortFrameList(frames: FrameMetadata[], sources: SourceRecord[]) {
  const order = new Map(
    sources.map((source, index) => [source.id, source.position || index + 1]),
  );
  return [...frames].sort(
    (a, b) =>
      (order.get(a.source_id) || 99999) - (order.get(b.source_id) || 99999) ||
      (a.review_order ?? 99999) - (b.review_order ?? 99999) ||
      a.timestamp - b.timestamp ||
      a.index - b.index,
  );
}

function buildRequest(draft: DraftConfig, sources: string[]): JobRequest {
  return {
    sources,
    interval: Number(draft.interval),
    start_time: Number(draft.startTime),
    end_time: draft.endTime.trim() ? Number(draft.endTime) : null,
    max_frames: Number(draft.maxFrames),
    dedupe_mode: draft.dedupeMode,
    ocr_enabled: draft.ocrEnabled,
    phash_distance: Number(draft.phashDistance),
    include_first: draft.includeFirst,
    include_last: draft.includeLast,
    scene_threshold: Number(draft.sceneThreshold || 0),
    title: draft.title.trim() || null,
  };
}

function buildExportRequest(draft: DraftConfig): ExportRequest {
  const imagesOnly = draft.imagesOnly;
  const pptxOnly = draft.formats.length === 1 && draft.formats[0] === "pptx";
  return {
    images_only: imagesOnly,
    formats: draft.formats,
    layout: imagesOnly || pptxOnly ? "single" : draft.layout,
    columns: imagesOnly || pptxOnly
      ? 1
      : draft.layout === "four"
        ? 4
        : draft.layout === "two"
          ? 2
          : 1,
    include_cover: imagesOnly ? false : draft.includeCover,
    include_timestamps: imagesOnly ? false : draft.includeTimestamps,
    include_source_titles: imagesOnly ? false : draft.includeSourceTitles,
    title: draft.title.trim() || "FrameNotes AI",
    include_ocr_text: imagesOnly ? false : draft.includeOcrText,
    include_sections: imagesOnly ? false : draft.includeSections,
    separate_videos: draft.separateVideos,
    page_size: imagesOnly ? undefined : draft.pageSize,
    orientation: imagesOnly ? undefined : draft.orientation,
    margin_mm: imagesOnly ? undefined : draft.marginMm,
    aspect_ratio: imagesOnly ? undefined : draft.aspectRatio,
    footer: imagesOnly ? "" : draft.footer,
  };
}

export default function HomePage() {
  const [phase, setPhase] = useState<Phase>("landing");
  const [mode, setMode] = useState<ProcessingMode>("extract");
  const [sourceMode, setSourceMode] = useState<SourceMode>("links");
  const [sourceText, setSourceText] = useState("");
  const [upload, setUpload] = useState<File | null>(null);
  const [draft, setDraft] = useState<DraftConfig>(initialConfig);
  const [analysis, setAnalysis] = useState<AnalyzedSource[]>([]);
  const [job, setJob] = useState<JobRecord | null>(null);
  const [frames, setFrames] = useState<FrameMetadata[]>([]);
  const [downloads, setDownloads] = useState<DownloadArtifact[]>([]);
  const [recentJobs, setRecentJobs] = useState<JobSummary[]>([]);
  const [health, setHealth] = useState<"unknown" | "ok" | "offline">("unknown");
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [processingError, setProcessingError] = useState<string | null>(null);
  const [connectionBanner, setConnectionBanner] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exportSnapshot, setExportSnapshot] = useState<ExportRequest | null>(
    null,
  );
  const [lastExportRequest, setLastExportRequest] =
    useState<ExportRequest | null>(null);

  const sources = useMemo(() => parseSources(sourceText), [sourceText]);

  const refreshJobs = useCallback(async () => {
    try {
      const result = await getJobs();
      setRecentJobs(result.jobs);
      setHealth("ok");
      return result.jobs;
    } catch (caught) {
      setHealth("offline");
      throw caught;
    }
  }, []);

  const refreshReview = useCallback(async (jobId: string) => {
    const result = await getAllFrames(jobId, 6000);
    setFrames(result.frames);
    setSelected(new Set());
    if (result.total > result.frames.length)
      setNotice(
        `This job has ${result.total.toLocaleString()} frames. Showing the first 6,000 for a safe browser review.`,
      );
  }, []);

  const storeCurrentJob = useCallback((record: JobRecord) => {
    setJob(record);
    if (typeof window !== "undefined")
      window.localStorage.setItem("framenotes-current-job", record.id);
  }, []);

  const markJobEdited = useCallback((jobId: string) => {
    if (typeof window !== "undefined")
      window.localStorage.setItem(`framenotes-edited:${jobId}`, "1");
  }, []);

  const openRecord = useCallback(
    async (record: JobRecord) => {
      const locallyEdited =
        typeof window !== "undefined" &&
        window.localStorage.getItem(`framenotes-edited:${record.id}`) === "1";
      const normalized = locallyEdited ? { ...record, downloads: [] } : record;
      storeCurrentJob(normalized);
      setLastExportRequest(null);
      setError(null);
      setProcessingError(null);
      setConnectionBanner(null);
      if (
        normalized.status === "exporting" ||
        normalized.stage.toLowerCase().includes("exporting")
      ) {
        setMode("export");
        setPhase("processing");
        return;
      }
      if (
        normalized.status === "queued" ||
        normalized.status === "running" ||
        normalized.status === "cancelling"
      ) {
        setMode("extract");
        setPhase("processing");
        return;
      }
      if (
        normalized.status === "failed" &&
        !normalized.stage.toLowerCase().includes("export")
      ) {
        setProcessingError(
          normalized.error ||
            "Processing failed. Adjust the source or settings and try again.",
        );
        setMode("extract");
        setPhase("processing");
        return;
      }
      if (
        (normalized.status === "failed" || normalized.stage.toLowerCase() === "export failed") &&
        normalized.stage.toLowerCase().includes("export")
      ) {
        setError(
          normalized.error || "Export failed. Your review is still available.",
        );
      }
      if (
        !locallyEdited &&
        normalized.status === "completed" &&
        normalized.stage.toLowerCase() === "exported" &&
        normalized.downloads.length
      ) {
        setDownloads(normalized.downloads);
        setPhase("results");
        return;
      }
      try {
        await refreshReview(record.id);
        setPhase("review");
      } catch (caught) {
        setError(
          caught instanceof Error
            ? caught.message
            : "This job's frames could not be loaded.",
        );
        setPhase("landing");
      }
    },
    [refreshReview, storeCurrentJob],
  );

  useEffect(() => {
    let disposed = false;
    const bootstrap = async () => {
      try {
        await initializeSession();
        const [healthResult, jobs] = await Promise.all([
          getHealth(),
          refreshJobs(),
        ]);
        if (disposed) return;
        setHealth(healthResult.status === "ok" ? "ok" : "offline");
        const savedId = window.localStorage.getItem("framenotes-current-job");
        const saved = savedId
          ? jobs.find((item) => item.id === savedId)
          : undefined;
        if (saved) {
          try {
            const record = await getJob(saved.id);
            if (!disposed) await openRecord(record);
          } catch {
            window.localStorage.removeItem("framenotes-current-job");
            if (!disposed)
              setNotice(
                "Your previous job is no longer available. Start a new project.",
              );
          }
        } else if (savedId) {
          window.localStorage.removeItem("framenotes-current-job");
          if (!disposed)
            setNotice("Your previous job has expired. Start a new project.");
        }
      } catch (caught) {
        if (disposed) return;
        setHealth("offline");
        setNotice(
          caught instanceof Error
            ? caught.message
            : "The backend is not reachable yet. You can still prepare a project.",
        );
      }
    };
    void bootstrap();
    return () => {
      disposed = true;
    };
  }, [openRecord, refreshJobs]);

  const beginConfiguration = async () => {
    setError(null);
    setNotice(null);
    if (sourceMode === "upload") {
      if (!upload) {
        setError("Choose a video or image file first.");
        return;
      }
      setAnalysis([]);
      setPhase("config");
      return;
    }
    if (!sources.length) {
      setError("Paste at least one real YouTube URL to continue.");
      return;
    }
    if (sources.length > 30) {
      setError(
        "Use at most 30 source lines per project. Playlists can expand on the backend.",
      );
      return;
    }
    setPending("analyze");
    try {
      const response = await analyzeSources(sources);
      setAnalysis(response.sources);
      setPhase("config");
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Link metadata could not be loaded.",
      );
      setPhase("config");
    } finally {
      setPending(null);
    }
  };

  const validateDraft = () => {
    const start = Number(draft.startTime);
    const end = draft.endTime.trim() ? Number(draft.endTime) : null;
    const scene = Number(draft.sceneThreshold || 0);
    const margin = Number(draft.marginMm);
    if (
      !Number.isFinite(draft.interval) ||
      draft.interval < 1 ||
      draft.interval > 60
    )
      return "Interval must be between 1 and 60 seconds.";
    if (
      !Number.isFinite(draft.maxFrames) ||
      draft.maxFrames < 1 ||
      draft.maxFrames > 3000
    )
      return "Maximum candidates must be between 1 and 3,000 per video.";
    if (!Number.isFinite(start) || start < 0)
      return "Start timestamp must be zero or greater.";
    if (end !== null && (!Number.isFinite(end) || end <= start))
      return "End timestamp must be greater than start timestamp.";
    if (!Number.isFinite(scene) || scene < 0 || scene > 1)
      return "Scene threshold must be between 0 and 1.";
    if (
      !Number.isFinite(draft.phashDistance) ||
      draft.phashDistance < 0 ||
      draft.phashDistance > 20
    )
      return "pHash distance must be between 0 and 20.";
    if (
      !draft.imagesOnly &&
      (!Number.isFinite(margin) || margin < 5 || margin > 30)
    )
      return "Margins must be between 5 and 30 mm.";
    if (!draft.formats.length) return "Choose at least one export format.";
    if (!draft.ownsContent || !draft.acceptsTerms)
      return "Confirm your permission to use the sources and accept the platform terms before starting.";
    return null;
  };

  const startJob = async () => {
    const validation = validateDraft();
    if (validation) {
      setError(validation);
      return;
    }
    setError(null);
    setProcessingError(null);
    setPending("start");
    const request = buildRequest(
      draft,
      sourceMode === "upload" ? ["upload"] : sources,
    );
    try {
      const created =
        sourceMode === "upload" && upload
          ? await uploadSource(upload, request)
          : await createJob(request);
      storeCurrentJob(created);
      setMode("extract");
      setPhase("processing");
      await refreshJobs().catch(() => undefined);
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not create this job.",
      );
    } finally {
      setPending(null);
    }
  };

  const activeJobId = job?.id;

  useEffect(() => {
    if (phase !== "processing" || !activeJobId) return;
    const jobId = activeJobId;
    const processingMode = mode;
    let disposed = false;
    let pollTimer: number | undefined;
    let polls = 0;
    let unsubscribe: () => void = () => undefined;

    const finish = async (record: JobRecord) => {
      if (disposed) return;
      storeCurrentJob(record);
      if (processingMode === "export") {
        if (
          record.status === "completed" &&
          record.stage.toLowerCase() === "exported"
        ) {
          setDownloads(record.downloads);
          setPhase("results");
          setExportSnapshot(null);
          window.localStorage.removeItem(`framenotes-edited:${record.id}`);
          await refreshJobs().catch(() => undefined);
        } else if (
          record.status === "failed" ||
          record.stage.toLowerCase() === "export failed"
        ) {
          setError(
            record.error || "Export failed. Your review is still available.",
          );
          setPhase("review");
          setExportSnapshot(null);
        } else if (record.status === "cancelled") {
          setNotice("Export cancelled. Your frame review is unchanged.");
          setPhase("review");
          setExportSnapshot(null);
        }
      } else if (record.status === "failed") {
        setProcessingError(
          record.error || "Processing failed. Check the source and try again.",
        );
      } else if (
        record.status === "completed" ||
        record.status === "cancelled"
      ) {
        try {
          await refreshReview(record.id);
          setPhase("review");
          await refreshJobs().catch(() => undefined);
        } catch (caught) {
          setProcessingError(
            caught instanceof Error
              ? caught.message
              : "The completed frames could not be loaded.",
          );
        }
      }
    };

    const handleRecord = (record: JobRecord) => {
      if (disposed) return;
      setJob((current) => (current ? { ...current, ...record } : record));
      const terminal =
        processingMode === "export"
          ? isExportTerminal(record)
          : isExtractionTerminal(record);
      if (terminal) {
        unsubscribe();
        if (pollTimer) window.clearInterval(pollTimer);
        void getJob(jobId)
          .then(finish)
          .catch(() => finish(record));
      }
    };

    unsubscribe = subscribeToProgress(
      jobId,
      (event: ProgressEvent) => {
        setConnectionBanner(null);
        setJob((current) =>
          current
            ? {
                ...current,
                status: event.status,
                progress: event.progress,
                stage: event.stage,
                frame_count: event.frame_count,
                duplicate_count: event.duplicate_count,
                retained_count: event.retained_count,
                ocr_status: event.ocr_status,
                error: event.error,
                warnings: event.warnings || current.warnings,
              }
            : current,
        );
        const terminal =
          processingMode === "export"
            ? event.status === "failed" ||
              event.status === "cancelled" ||
              (event.status === "completed" &&
                event.stage.toLowerCase().includes("export"))
            : event.status === "failed" ||
              event.status === "cancelled" ||
              event.status === "completed";
        if (terminal) {
          unsubscribe();
          if (pollTimer) window.clearInterval(pollTimer);
          void getJob(jobId)
            .then(finish)
            .catch(() => undefined);
        }
      },
      () => {
        if (!disposed) {
          setConnectionBanner(
            "Live updates disconnected. Checking the job status automatically.",
          );
          unsubscribe();
        }
      },
    );

    pollTimer = window.setInterval(() => {
      polls += 1;
      if (polls > 600) {
        if (pollTimer) window.clearInterval(pollTimer);
        setConnectionBanner(
          "Automatic status checks stopped after 15 minutes. Refresh or resume this job to continue.",
        );
        return;
      }
      void getJob(jobId)
        .then(handleRecord)
        .catch(() =>
          setConnectionBanner(
            "The backend is not responding. We will retry the status check.",
          ),
        );
    }, 1500);

    return () => {
      disposed = true;
      unsubscribe();
      if (pollTimer) window.clearInterval(pollTimer);
    };
  }, [activeJobId, mode, phase, refreshJobs, refreshReview, storeCurrentJob]);

  const cancelCurrentJob = async () => {
    if (!job || pending) return;
    setPending("cancel");
    try {
      const updated = await cancelJob(job.id);
      storeCurrentJob(updated);
      setConnectionBanner(
        "Cancellation requested. Waiting for the worker to finish safely.",
      );
    } catch (caught) {
      setProcessingError(
        caught instanceof Error ? caught.message : "Could not cancel this job.",
      );
    } finally {
      setPending(null);
    }
  };

  const toggleFrame = async (frame: FrameMetadata) => {
    if (!job || pending) return;
    setPending(`frame:${frame.id}`);
    try {
      const response = await updateFrameRetention(
        job.id,
        frame.id,
        !frame.retained,
      );
      storeCurrentJob({ ...response, downloads: [] });
      markJobEdited(job.id);
      setFrames((current) =>
        current.map((item) =>
          item.id === frame.id
            ? { ...item, retained: !frame.retained, retention_overridden: true }
            : item,
        ),
      );
      setDownloads([]);
      setNotice(
        "Frame change saved. Export files were cleared; export a fresh snapshot when ready.",
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not update this frame.",
      );
    } finally {
      setPending(null);
    }
  };

  const applyBulk = async (retained: boolean) => {
    if (!job || !selected.size || pending) return;
    setPending("bulk");
    try {
      const response = await bulkUpdateRetention(
        job.id,
        [...selected],
        retained,
      );
      storeCurrentJob({ ...response.job, downloads: [] });
      markJobEdited(job.id);
      setFrames(
        (response.job.frames || []).length
          ? response.job.frames || []
          : (current) =>
              current.map((frame) =>
                selected.has(frame.id)
                  ? { ...frame, retained, retention_overridden: true }
                  : frame,
              ),
      );
      setSelected(new Set());
      setDownloads([]);
      setNotice(
        "Selection saved. Export files were cleared; export a fresh snapshot when ready.",
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not update the selected frames.",
      );
    } finally {
      setPending(null);
    }
  };

  const moveFrame = async (frameId: string, direction: "up" | "down") => {
    if (!job || pending) return;
    const sorted = sortFrameList(frames, job.sources);
    const currentIndex = sorted.findIndex((frame) => frame.id === frameId);
    if (currentIndex < 0) return;
    const frame = sorted[currentIndex];
    const sameSource = sorted
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => item.source_id === frame.source_id)
      .map(({ index }) => index);
    const sourceIndex = sameSource.indexOf(currentIndex);
    const nextSourceIndex = sourceIndex + (direction === "up" ? -1 : 1);
    if (nextSourceIndex < 0 || nextSourceIndex >= sameSource.length) return;
    const next = [...sorted];
    [next[currentIndex], next[sameSource[nextSourceIndex]]] = [
      next[sameSource[nextSourceIndex]],
      next[currentIndex],
    ];
    setFrames(next);
    setPending("reorder");
    try {
      const response = await reorderFrames(
        job.id,
        next.map((item) => item.id),
      );
      storeCurrentJob({ ...response, downloads: [] });
      markJobEdited(job.id);
      if ((response.frames || []).length)
        setFrames(sortFrameList(response.frames || [], response.sources));
      setDownloads([]);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not save the frame order.",
      );
      setFrames(sorted);
    } finally {
      setPending(null);
    }
  };

  const exportJob = async (payload: ExportRequest) => {
    if (!job || pending || !job.retained_count) return;
    const request: ExportRequest = {
      ...payload,
      images_only: payload.images_only ?? draft.imagesOnly,
    };
    setPending("export");
    setError(null);
    setDownloads([]);
    setExportSnapshot(request);
    setLastExportRequest(request);
    try {
      const response = await createExport(job.id, request);
      setJob((current) =>
        current
          ? {
              ...current,
              status: response.status,
              stage: "exporting",
              downloads: [],
            }
          : current,
      );
      setMode("export");
      setPhase("processing");
    } catch (caught) {
      setExportSnapshot(null);
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not start the export.",
      );
    } finally {
      setPending(null);
    }
  };

  const resumeSummary = async (summary: JobSummary) => {
    if (pending) return;
    setPending(`resume:${summary.id}`);
    try {
      const record = await getJob(summary.id);
      await openRecord(record);
    } catch (caught) {
      if (
        caught instanceof Error &&
        /404|not found|expired/i.test(caught.message)
      ) {
        setRecentJobs((current) =>
          current.filter((item) => item.id !== summary.id),
        );
        setNotice("That job has expired and is no longer available.");
        window.localStorage.removeItem("framenotes-current-job");
      } else
        setError(
          caught instanceof Error
            ? caught.message
            : "Could not resume this job.",
        );
    } finally {
      setPending(null);
    }
  };

  const removeSummary = async (summary: JobSummary) => {
    if (
      pending ||
      !window.confirm("Delete this saved job and its local artifacts?")
    )
      return;
    setPending(`delete:${summary.id}`);
    try {
      await deleteJob(summary.id);
      setRecentJobs((current) =>
        current.filter((item) => item.id !== summary.id),
      );
      if (window.localStorage.getItem("framenotes-current-job") === summary.id)
        window.localStorage.removeItem("framenotes-current-job");
      window.localStorage.removeItem(`framenotes-edited:${summary.id}`);
      if (job?.id === summary.id) reset();
    } catch (caught) {
      setError(
        caught instanceof Error ? caught.message : "Could not delete this job.",
      );
    } finally {
      setPending(null);
    }
  };

  const reset = () => {
    setPhase("landing");
    setMode("extract");
    setJob(null);
    setFrames([]);
    setDownloads([]);
    setAnalysis([]);
    setSelected(new Set());
    setExportSnapshot(null);
    setLastExportRequest(null);
    setSourceText("");
    setUpload(null);
    setDraft(initialConfig);
    setError(null);
    setProcessingError(null);
    setConnectionBanner(null);
    window.localStorage.removeItem("framenotes-current-job");
  };

  return (
    <main className="min-h-screen pb-16">
      <Header phase={phase} health={health} onHome={reset} />
      {phase !== "landing" ? (
        <WorkflowBar phase={phase} onHome={reset} />
      ) : null}
      <div className="shell pt-8 md:pt-12">
        {notice ? (
          <Notice message={notice} onClose={() => setNotice(null)} />
        ) : null}
        {phase === "landing" ? (
          <Landing
            sourceMode={sourceMode}
            setSourceMode={setSourceMode}
            sourceText={sourceText}
            setSourceText={setSourceText}
            upload={upload}
            setUpload={setUpload}
            onContinue={beginConfiguration}
            pending={pending === "analyze"}
            error={error}
            health={health}
            jobs={recentJobs}
            onResume={resumeSummary}
            onDelete={removeSummary}
            jobsPending={pending === "jobs"}
            onRefreshJobs={async () => {
              setPending("jobs");
              try {
                await refreshJobs();
              } catch (caught) {
                setError(
                  caught instanceof Error
                    ? caught.message
                    : "Could not refresh recent jobs.",
                );
              } finally {
                setPending(null);
              }
            }}
          />
        ) : null}
        {phase === "config" ? (
          <Configuration
            sourceMode={sourceMode}
            upload={upload}
            sources={sources}
            analysis={analysis}
            draft={draft}
            setDraft={setDraft}
            onBack={() => setPhase("landing")}
            onStart={startJob}
            pending={pending === "start"}
            error={error}
          />
        ) : null}
        {phase === "processing" && job ? (
          <Processing
            job={job}
            mode={mode}
            error={processingError || error}
            connectionBanner={connectionBanner}
            exportSnapshot={exportSnapshot}
            onCancel={cancelCurrentJob}
            onBack={() => (mode === "export" ? setPhase("review") : reset())}
            pending={pending === "cancel"}
          />
        ) : null}
        {phase === "review" && job ? (
          <Review
            job={job}
            frames={frames}
            selected={selected}
            setSelected={setSelected}
            onToggle={toggleFrame}
            onBulk={applyBulk}
            onMove={moveFrame}
            draft={draft}
            setDraft={setDraft}
            onExport={exportJob}
            onBack={() => setPhase("config")}
            error={error}
            pending={pending}
          />
        ) : null}
        {phase === "results" && job ? (
          <Results
            job={job}
            downloads={downloads.length ? downloads : job.downloads}
            exportRequest={lastExportRequest}
            onNew={reset}
            onReview={() => setPhase("review")}
            onRefresh={async () => {
              const updated = await getJob(job.id);
              storeCurrentJob(updated);
              setDownloads(updated.downloads);
            }}
          />
        ) : null}
      </div>
    </main>
  );
}

function Header({
  phase,
  health,
  onHome,
}: {
  phase: Phase;
  health: "unknown" | "ok" | "offline";
  onHome: () => void;
}) {
  return (
    <header className="shell flex items-center justify-between border-b border-line/80 py-5">
      <button type="button" onClick={onHome} aria-label="Go to FrameNotes home">
        <BrandMark />
      </button>
      <div className="flex items-center gap-2 sm:gap-4">
        <div className="hidden items-center gap-2 text-xs font-semibold text-muted md:flex">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              health === "ok"
                ? "bg-[#3d9d67]"
                : health === "offline"
                  ? "bg-[#c55b5b]"
                  : "bg-[#c49a34]",
            )}
          />
          {health === "ok"
            ? "Backend ready"
            : health === "offline"
              ? "Backend offline"
              : "Connecting"}
        </div>
        <ThemeToggle />
        <button
          type="button"
          className="btn-secondary !px-4 !py-2.5"
          onClick={onHome}
        >
          {phase === "landing" ? "Home" : "New project"}
        </button>
      </div>
    </header>
  );
}

function WorkflowBar({ phase, onHome }: { phase: Phase; onHome: () => void }) {
  const current =
    { config: 2, processing: 3, review: 4, results: 5 }[
      phase as Exclude<Phase, "landing">
    ] || 1;
  return (
    <div className="shell flex items-center justify-between gap-4 pt-6">
      <button type="button" className="btn-ghost -ml-3" onClick={onHome}>
        <ChevronLeft size={16} /> Start over
      </button>
      <div
        className="hidden items-center gap-5 sm:flex"
        aria-label="Workflow progress"
      >
        <StepPill
          index={1}
          label="Source"
          done={current > 1}
          active={current === 1}
        />
        <span className="h-px w-8 bg-line" />
        <StepPill
          index={2}
          label="Configure"
          done={current > 2}
          active={current === 2}
        />
        <span className="h-px w-8 bg-line" />
        <StepPill
          index={3}
          label="Process"
          done={current > 3}
          active={current === 3}
        />
        <span className="h-px w-8 bg-line" />
        <StepPill
          index={4}
          label="Review"
          done={current > 4}
          active={current === 4}
        />
        <span className="h-px w-8 bg-line" />
        <StepPill index={5} label="Export" active={current === 5} />
      </div>
      <span className="chip sm:hidden">Step {current} / 5</span>
    </div>
  );
}

function Landing({
  sourceMode,
  setSourceMode,
  sourceText,
  setSourceText,
  upload,
  setUpload,
  onContinue,
  pending,
  error,
  health,
  jobs,
  onResume,
  onDelete,
  jobsPending,
  onRefreshJobs,
}: {
  sourceMode: SourceMode;
  setSourceMode: (mode: SourceMode) => void;
  sourceText: string;
  setSourceText: (value: string) => void;
  upload: File | null;
  setUpload: (file: File | null) => void;
  onContinue: () => void;
  pending: boolean;
  error: string | null;
  health: "unknown" | "ok" | "offline";
  jobs: JobSummary[];
  onResume: (job: JobSummary) => void;
  onDelete: (job: JobSummary) => void;
  jobsPending: boolean;
  onRefreshJobs: () => void;
}) {
  return (
    <section className="pb-14 pt-8 lg:pt-14">
      <div className="grid items-start gap-12 lg:grid-cols-[1.05fr_.95fr] lg:gap-20">
        <div className="pt-3">
          <div className="mb-6 inline-flex items-center gap-2 rounded-full border border-[#c9e8d5] bg-[#effbf3] px-3 py-1.5 text-xs font-bold text-[#27704a]">
            <Sparkles size={14} /> Turn long videos into visual notes
          </div>
          <h1 className="max-w-3xl text-5xl font-black leading-[1.02] tracking-[-0.065em] text-ink sm:text-6xl xl:text-[78px]">
            Keep the ideas.
            <br />
            <span className="text-muted">Skip the repeats.</span>
          </h1>
          <p className="mt-7 max-w-xl text-lg leading-8 text-muted">
            FrameNotes extracts useful frames from real sources, lets you review
            every decision, and creates notes you can download.
          </p>
          <div className="mt-9 flex flex-wrap gap-x-6 gap-y-3 text-sm font-semibold text-muted">
            <span className="flex items-center gap-2">
              <Check size={16} className="text-[#3d9d67]" /> Chronological
            </span>
            <span className="flex items-center gap-2">
              <Check size={16} className="text-[#3d9d67]" /> Duplicate-aware
            </span>
            <span className="flex items-center gap-2">
              <Check size={16} className="text-[#3d9d67]" /> Export-ready
            </span>
          </div>
          <div className="mt-12 grid gap-4 border-t border-line pt-8 sm:grid-cols-3">
            <Feature
              icon={Layers3}
              title="Layered cleanup"
              text="Exact, perceptual, and structural comparisons preserve meaningful changes."
            />
            <Feature
              icon={Gauge}
              title="Long-video ready"
              text="Bounded sampling keeps browser review responsive."
            />
            <Feature
              icon={WandSparkles}
              title="Your final say"
              text="Restore or remove any frame before you freeze an export."
            />
          </div>
        </div>
        <div className="space-y-5">
          <div className="panel p-2 sm:p-3">
            <div className="rounded-[22px] border border-line bg-panel p-5 sm:p-7">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="eyebrow">Start a project</p>
                  <h2 className="mt-2 text-2xl font-black tracking-[-0.04em] text-ink">
                    What should we capture?
                  </h2>
                </div>
                <div className="flex h-11 w-11 items-center justify-center rounded-2xl bg-mint text-forest">
                  <Link2 size={20} />
                </div>
              </div>
              <div
                className="mt-7 flex rounded-full bg-forest/5 p-1"
                role="tablist"
                aria-label="Source type"
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={sourceMode === "links"}
                  onClick={() => setSourceMode("links")}
                  className={cn(
                    "flex flex-1 items-center justify-center gap-2 rounded-full px-3 py-2.5 text-xs font-bold",
                    sourceMode === "links"
                      ? "bg-panel text-ink shadow-sm"
                      : "text-muted",
                  )}
                >
                  <Link2 size={14} /> YouTube links
                </button>
                <button
                  type="button"
                  role="tab"
                  aria-selected={sourceMode === "upload"}
                  onClick={() => setSourceMode("upload")}
                  className={cn(
                    "flex flex-1 items-center justify-center gap-2 rounded-full px-3 py-2.5 text-xs font-bold",
                    sourceMode === "upload"
                      ? "bg-panel text-ink shadow-sm"
                      : "text-muted",
                  )}
                >
                  <Upload size={14} /> Local upload
                </button>
              </div>
              {sourceMode === "links" ? (
                <label className="mt-7 block">
                  <span className="label">Video or playlist links</span>
                  <textarea
                    value={sourceText}
                    onChange={(event) => setSourceText(event.target.value)}
                    className="input-control min-h-[178px] resize-y leading-6"
                    placeholder={
                      "https://www.youtube.com/watch?v=...\nhttps://youtu.be/...\n\nOne real URL per line"
                    }
                  />
                  <span className="mt-3 flex items-center gap-1.5 text-xs text-muted">
                    <ClipboardPaste size={14} /> Up to 30 source lines ·
                    duplicates removed
                  </span>
                </label>
              ) : (
                <label className="mt-7 block cursor-pointer">
                  <span className="label">Video or image file</span>
                  <span className="flex min-h-[178px] flex-col items-center justify-center rounded-2xl border border-dashed border-line bg-forest/[0.025] p-6 text-center transition hover:border-forest/40">
                    <HardDriveUpload size={28} className="text-forest" />
                    <span className="mt-3 text-sm font-bold text-ink">
                      {upload ? upload.name : "Choose a file from this browser"}
                    </span>
                    <span className="mt-1 text-xs text-muted">
                      {upload
                        ? `${formatBytes(upload.size)} · ${upload.type || "media"}`
                        : "Metadata is optional; configuration starts immediately."}
                    </span>
                    <input
                      type="file"
                      accept="video/*,image/*"
                      className="sr-only"
                      onChange={(event) =>
                        setUpload(event.target.files?.[0] || null)
                      }
                    />
                  </span>
                </label>
              )}
              {error ? <InlineError message={error} /> : null}
              {health === "offline" ? (
                <div
                  role="status"
                  className="mt-4 rounded-2xl border border-[#ead49b] bg-[#fff9e8] p-3 text-xs leading-5 text-[#755a18]"
                >
                  Backend health check is unavailable. You can retry now; no
                  source is submitted until you start.
                </div>
              ) : null}
              <button
                type="button"
                onClick={onContinue}
                disabled={pending}
                className="btn-primary mt-6 w-full py-3.5"
              >
                {pending ? (
                  <LoadingLabel>Reading source metadata</LoadingLabel>
                ) : sourceMode === "links" ? (
                  <>
                    Check links & configure <ArrowRight size={17} />
                  </>
                ) : (
                  <>
                    Configure upload <ArrowRight size={17} />
                  </>
                )}
              </button>
              <p className="mt-4 text-center text-[11px] leading-5 text-muted">
                Use only media you own or are permitted to process. The backend
                validates source availability before extraction.
              </p>
            </div>
          </div>
          <RecentJobs
            jobs={jobs}
            onResume={onResume}
            onDelete={onDelete}
            pending={jobsPending}
            onRefresh={onRefreshJobs}
          />
        </div>
      </div>
    </section>
  );
}

function Feature({
  icon: Icon,
  title,
  text,
}: {
  icon: LucideIcon;
  title: string;
  text: string;
}) {
  return (
    <div className="flex gap-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-panel text-forest shadow-sm">
        <Icon size={18} />
      </div>
      <div>
        <h3 className="text-sm font-bold text-ink">{title}</h3>
        <p className="mt-1 text-sm leading-6 text-muted">{text}</p>
      </div>
    </div>
  );
}

function RecentJobs({
  jobs,
  onResume,
  onDelete,
  pending,
  onRefresh,
}: {
  jobs: JobSummary[];
  onResume: (job: JobSummary) => void;
  onDelete: (job: JobSummary) => void;
  pending: boolean;
  onRefresh: () => void;
}) {
  return (
    <section className="panel p-5" aria-labelledby="recent-jobs-title">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="eyebrow">Local workspace</p>
          <h2
            id="recent-jobs-title"
            className="mt-1 text-lg font-black text-ink"
          >
            Recent jobs
          </h2>
        </div>
        <button
          type="button"
          className="btn-ghost !h-9 !w-9 !p-0"
          aria-label="Refresh recent jobs"
          onClick={onRefresh}
          disabled={pending}
        >
          <RefreshCw size={15} className={pending ? "animate-spin" : ""} />
        </button>
      </div>
      {jobs.length ? (
        <div className="mt-4 space-y-2">
          {jobs.slice(0, 6).map((item) => (
            <div
              key={item.id}
              className="flex items-center gap-3 rounded-2xl border border-line bg-panel p-3"
            >
              <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-forest/5 text-forest">
                <Film size={16} />
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-bold text-ink">
                  {item.stage || "FrameNotes job"}
                </p>
                <p className="mt-1 text-xs text-muted">
                  {item.retained_count} retained · {humanStatus(item.status)}
                  {item.downloads.length
                    ? ` · ${item.downloads.length} export${item.downloads.length === 1 ? "" : "s"}`
                    : ""}
                </p>
              </div>
              <button
                type="button"
                className="btn-ghost !px-2.5 !py-1.5 text-xs"
                onClick={() => onResume(item)}
                disabled={pending}
              >
                Resume
              </button>
              <button
                type="button"
                className="btn-ghost !h-8 !w-8 !p-0 text-muted hover:text-[#a84141]"
                aria-label={`Delete job ${item.id.slice(0, 8)}`}
                onClick={() => onDelete(item)}
                disabled={pending}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      ) : (
        <div className="mt-4 rounded-2xl border border-dashed border-line p-5 text-center">
          <Clock3 size={20} className="mx-auto text-muted" />
          <p className="mt-2 text-sm font-bold text-ink">No recent jobs</p>
          <p className="mt-1 text-xs text-muted">
            Completed and in-progress work appears here after you start.
          </p>
        </div>
      )}
    </section>
  );
}

function Configuration({
  sourceMode,
  upload,
  sources,
  analysis,
  draft,
  setDraft,
  onBack,
  onStart,
  pending,
  error,
}: {
  sourceMode: SourceMode;
  upload: File | null;
  sources: string[];
  analysis: AnalyzedSource[];
  draft: DraftConfig;
  setDraft: React.Dispatch<React.SetStateAction<DraftConfig>>;
  onBack: () => void;
  onStart: () => void;
  pending: boolean;
  error: string | null;
}) {
  const update = <K extends keyof DraftConfig>(key: K, value: DraftConfig[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const toggleFormat = (format: "pdf" | "pptx") =>
    setDraft((current) => ({
      ...current,
      formats: current.formats.includes(format)
        ? current.formats.filter((item) => item !== format)
        : [...current.formats, format],
    }));
  return (
    <section className="pb-16">
      <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="eyebrow">Step 2 · Configure</p>
          <h1 className="mt-2 text-4xl font-black tracking-[-0.06em] text-ink sm:text-5xl">
            Shape the output.
          </h1>
          <p className="mt-3 max-w-xl text-sm leading-6 text-muted">
            Sampling and deduplication are bounded by the backend. Advanced
            controls stay explicit so you know what will run.
          </p>
        </div>
        <div className="chip">
          {sourceMode === "upload" ? <Upload size={14} /> : <Video size={14} />}{" "}
          {sourceMode === "upload"
            ? upload?.name || "Upload"
            : `${sources.length} source${sources.length === 1 ? "" : "s"}`}
        </div>
      </div>
      <div className="grid gap-6 lg:grid-cols-[1.08fr_.92fr]">
        <div className="panel p-5 sm:p-7">
          {sourceMode === "upload" && upload ? (
            <div className="mb-7 flex items-center gap-3 rounded-2xl border border-line bg-forest/[0.025] p-4">
              <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-mint text-forest">
                <FileImage size={18} />
              </div>
              <div className="min-w-0">
                <p className="truncate text-sm font-bold text-ink">
                  {upload.name}
                </p>
                <p className="mt-1 text-xs text-muted">
                  {formatBytes(upload.size)} · upload metadata will be read by
                  the worker
                </p>
              </div>
            </div>
          ) : null}
          {analysis.length ? <SourceSummary sources={analysis} /> : null}
          <SectionHeader
            icon={Film}
            title="Frame extraction"
            text="Choose where the visual story begins and ends."
          />
          <div className="mt-7 grid gap-5 sm:grid-cols-2">
            <label>
              <span className="label flex items-center gap-1">
                Interval{" "}
                <HelpTip text="A candidate image is sampled every N seconds." />
              </span>
              <div className="flex items-center gap-3">
                <input
                  aria-label="Sampling interval"
                  type="range"
                  min="1"
                  max="60"
                  value={draft.interval}
                  onChange={(event) =>
                    update("interval", Number(event.target.value))
                  }
                  className="w-full accent-[#143c2c]"
                />
                <span className="w-14 rounded-xl bg-forest/5 px-2 py-2 text-center text-sm font-bold text-ink">
                  {draft.interval}s
                </span>
              </div>
              <span className="mt-2 block text-xs text-muted">
                1–60 seconds
              </span>
            </label>
            <label>
              <span className="label">Maximum candidates per video</span>
              <input
                type="number"
                min={1}
                max={3000}
                value={draft.maxFrames}
                onChange={(event) =>
                  update("maxFrames", Number(event.target.value))
                }
                className="input-control"
              />
              <span className="mt-2 block text-xs text-muted">
                1–3,000 per video
              </span>
            </label>
            <label>
              <span className="label">Start timestamp</span>
              <input
                type="number"
                min={0}
                step={1}
                value={draft.startTime}
                onChange={(event) => update("startTime", event.target.value)}
                className="input-control"
                placeholder="0"
              />
            </label>
            <label>
              <span className="label">
                End timestamp{" "}
                <span className="font-normal normal-case tracking-normal text-muted">
                  optional
                </span>
              </span>
              <input
                type="number"
                min={0}
                step={1}
                value={draft.endTime}
                onChange={(event) => update("endTime", event.target.value)}
                className="input-control"
                placeholder="Full duration"
              />
            </label>
          </div>
          <div className="mt-7 border-t border-line pt-6">
            <SectionHeader
              icon={WandSparkles}
              title="Deduplication"
              text="Conservative by default. When uncertain, FrameNotes keeps the frame."
            />
          </div>
          <div className="mt-4 grid gap-3 sm:grid-cols-2">
            {(
              [
                [
                  "strict",
                  "Strict",
                  "Only exact and very close visual repeats",
                ],
                ["balanced", "Balanced", "Recommended for teaching and talks"],
                ["aggressive", "Aggressive", "Fewer frames, higher loss risk"],
                [
                  "manual",
                  "Manual review",
                  "Keep near-duplicates for your decision",
                ],
              ] as const
            ).map(([value, label, text]) => (
              <button
                type="button"
                key={value}
                aria-pressed={draft.dedupeMode === value}
                onClick={() => update("dedupeMode", value)}
                className={cn(
                  "rounded-2xl border p-4 text-left transition",
                  draft.dedupeMode === value
                    ? "border-forest bg-mint/30 ring-2 ring-forest/10"
                    : "border-line bg-panel hover:border-forest/25",
                )}
              >
                <span className="flex items-center justify-between gap-2 text-sm font-bold text-ink">
                  {label}
                  {draft.dedupeMode === value ? <Check size={16} /> : null}
                </span>
                <span className="mt-1 block text-xs leading-5 text-muted">
                  {text}
                </span>
              </button>
            ))}
          </div>
          {draft.dedupeMode === "aggressive" ? (
            <div className="mt-4 flex gap-2 rounded-2xl border border-[#ead49b] bg-[#fff9e8] p-3 text-xs leading-5 text-[#755a18]">
              <AlertCircle size={16} className="mt-0.5 shrink-0" />
              Aggressive mode can remove meaningful visual changes. Review the
              suspected tab before exporting.
            </div>
          ) : null}
          <label className="mt-5 flex cursor-pointer items-center gap-3 rounded-2xl border border-line p-4">
            <input
              type="checkbox"
              checked={draft.ocrEnabled}
              onChange={(event) => update("ocrEnabled", event.target.checked)}
              className="h-4 w-4 accent-[#143c2c]"
            />
            <span>
              <span className="block text-sm font-bold text-ink">
                Try local OCR
              </span>
              <span className="block text-xs text-muted">
                Uses the backend OCR tool when installed.
              </span>
            </span>
          </label>
          <details className="mt-6 rounded-2xl border border-line p-4">
            <summary className="cursor-pointer list-none text-sm font-bold text-ink">
              <span className="flex items-center justify-between">
                Advanced extraction options{" "}
                <ChevronDown size={16} className="text-muted" />
              </span>
            </summary>
            <div className="mt-5 grid gap-5 border-t border-line pt-5 sm:grid-cols-2">
              <label>
                <span className="label">pHash distance</span>
                <input
                  type="number"
                  min={0}
                  max={20}
                  value={draft.phashDistance}
                  onChange={(event) =>
                    update("phashDistance", Number(event.target.value))
                  }
                  className="input-control"
                />
                <span className="mt-2 block text-xs text-muted">
                  Default 6 · lower is stricter
                </span>
              </label>
              <label>
                <span className="label">Scene threshold</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={draft.sceneThreshold}
                  onChange={(event) =>
                    update("sceneThreshold", event.target.value)
                  }
                  className="input-control"
                />
                <span className="mt-2 block text-xs text-muted">
                  0 disables this guard
                </span>
              </label>
              <Toggle
                checked={draft.includeFirst}
                onChange={(value) => update("includeFirst", value)}
                label="Include first frame"
              />
              <Toggle
                checked={draft.includeLast}
                onChange={(value) => update("includeLast", value)}
                label="Include last frame"
              />
            </div>
          </details>
        </div>
        <div className="space-y-6">
          <div className="panel p-5 sm:p-7">
            <SectionHeader
              icon={FileText}
              title="Export formats"
              text={
                draft.imagesOnly
                  ? "Choose PDF or PowerPoint; each retained frame becomes its own image page or slide."
                  : "The export starts after you review and freeze your selection."
              }
            />
            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              <FormatChoice
                active={draft.formats.includes("pdf")}
                onClick={() => toggleFormat("pdf")}
                icon={FileText}
                title="PDF"
                text={draft.imagesOnly ? "One image per page" : "Notes with selectable text"}
              />
              <FormatChoice
                active={draft.formats.includes("pptx")}
                onClick={() => toggleFormat("pptx")}
                icon={Layers3}
                title="PowerPoint"
                text={draft.imagesOnly ? "One image per slide" : "Always one image per slide"}
              />
            </div>
            {draft.imagesOnly ? (
              <div className="mt-6 rounded-2xl border border-forest/20 bg-mint/20 p-4">
                <p className="text-sm font-bold text-ink">Images-only output</p>
                <p className="mt-1 text-xs leading-5 text-muted">
                  One image per PDF page or PowerPoint slide, with no added text:
                  no cover, contents, labels, footer, timestamps, page numbers, or
                  OCR. PDF pages match each image&apos;s aspect ratio with no margins.
                </p>
              </div>
            ) : (
              <>
                <div className="mt-6 grid gap-4">
                  <SelectControl
                    label="PDF layout"
                    value={draft.layout}
                    onChange={(value) => update("layout", value as ExportLayout)}
                    options={[
                      { value: "single", label: "Single image" },
                      { value: "two", label: "Two images" },
                      { value: "four", label: "Four images" },
                      { value: "contact", label: "Contact sheet" },
                      { value: "notes", label: "Notes" },
                    ]}
                  />
                  <div className="grid gap-4 sm:grid-cols-2">
                    <SelectControl
                      label="Page size"
                      value={draft.pageSize}
                      onChange={(value) =>
                        update("pageSize", value as "A4" | "Letter")
                      }
                      options={[
                        { value: "A4", label: "A4" },
                        { value: "Letter", label: "Letter" },
                      ]}
                    />
                    <SelectControl
                      label="PDF orientation"
                      value={draft.orientation}
                      onChange={(value) =>
                        update("orientation", value as "portrait" | "landscape")
                      }
                      options={[
                        { value: "portrait", label: "Portrait" },
                        { value: "landscape", label: "Landscape" },
                      ]}
                    />
                  </div>
                </div>
                <p className="mt-4 text-xs leading-5 text-muted">
                  PowerPoint output is always one image per slide, regardless of the
                  PDF layout.
                </p>
              </>
            )}
          </div>
          <div className="panel p-5 sm:p-7">
            <SectionHeader
              icon={FolderOpen}
              title="Document details"
              text="Freeze the options used for this export."
            />
            <div className="mt-6 rounded-2xl border border-line bg-forest/[0.025] p-4">
              <Toggle
                checked={draft.imagesOnly}
                onChange={(value) => update("imagesOnly", value)}
                label="Images only"
              />
              <p className="mt-2 text-xs leading-5 text-muted">
                {draft.imagesOnly
                  ? "One image per page or slide with no cover, contents, labels, footer, timestamps, page numbers, or OCR. PDF pages match the image aspect with no margins."
                  : "Off: annotated PDF and PowerPoint options are enabled below, including layout and text settings."}
              </p>
            </div>
            <label className="mt-6 block">
              <span className="label">
                {draft.imagesOnly ? "Project title (filename only)" : "Project title"}
              </span>
              <input
                value={draft.title}
                onChange={(event) => update("title", event.target.value)}
                className="input-control"
                maxLength={200}
              />
              {draft.imagesOnly ? (
                <span className="mt-2 block text-xs text-muted">
                  Used for the downloaded filename; it is not placed on a page or
                  slide.
                </span>
              ) : null}
            </label>
            <div className="mt-5 space-y-3">
              {!draft.imagesOnly ? (
                <>
                  <div className="grid gap-4 sm:grid-cols-2">
                    <label>
                      <span className="label">Aspect ratio</span>
                      <select
                        className="input-control"
                        value={draft.aspectRatio}
                        onChange={(event) =>
                          update(
                            "aspectRatio",
                            event.target.value as "16:9" | "4:3",
                          )
                        }
                      >
                        <option value="16:9">16:9</option>
                        <option value="4:3">4:3</option>
                      </select>
                    </label>
                    <label>
                      <span className="label">Margins (mm)</span>
                      <input
                        type="number"
                        min={5}
                        max={30}
                        value={draft.marginMm}
                        onChange={(event) =>
                          update("marginMm", Number(event.target.value))
                        }
                        className="input-control"
                      />
                    </label>
                  </div>
                  <label className="block">
                    <span className="label">Footer</span>
                    <input
                      value={draft.footer}
                      onChange={(event) => update("footer", event.target.value)}
                      className="input-control"
                      maxLength={100}
                    />
                  </label>
                  <Toggle
                    checked={draft.includeCover}
                    onChange={(value) => update("includeCover", value)}
                    label="Add cover page / title slide"
                  />
                  <Toggle
                    checked={draft.includeTimestamps}
                    onChange={(value) => update("includeTimestamps", value)}
                    label="Show frame timestamps"
                  />
                  <Toggle
                    checked={draft.includeSourceTitles}
                    onChange={(value) => update("includeSourceTitles", value)}
                    label="Show source titles"
                  />
                  <Toggle
                    checked={draft.includeOcrText}
                    onChange={(value) => update("includeOcrText", value)}
                    label="Include OCR text"
                  />
                  <Toggle
                    checked={draft.includeSections}
                    onChange={(value) => update("includeSections", value)}
                    label="Include source sections"
                  />
                </>
              ) : null}
              <Toggle
                checked={draft.separateVideos}
                onChange={(value) => update("separateVideos", value)}
                label="Separate videos in output"
              />
            </div>
          </div>
          <div className="panel p-5 sm:p-7">
            <p className="eyebrow">Before you start</p>
            <div className="mt-4 space-y-3">
              <CheckField
                checked={draft.ownsContent}
                onChange={(value) => update("ownsContent", value)}
              >
                I own these sources or have permission to process them.
              </CheckField>
              <CheckField
                checked={draft.acceptsTerms}
                onChange={(value) => update("acceptsTerms", value)}
              >
                I agree to follow the source platform’s terms and applicable
                laws.
              </CheckField>
            </div>
          </div>
          {error ? <InlineError message={error} /> : null}
          <div className="flex items-center justify-between gap-3">
            <button
              type="button"
              className="btn-ghost"
              onClick={onBack}
              disabled={pending}
            >
              <ArrowLeft size={16} /> Change sources
            </button>
            <button
              type="button"
              className="btn-primary"
              disabled={pending || !draft.formats.length}
              onClick={onStart}
            >
              {pending ? (
                <LoadingLabel>Starting job</LoadingLabel>
              ) : (
                <>
                  Start processing <Play size={15} fill="currentColor" />
                </>
              )}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

function SourceSummary({ sources }: { sources: AnalyzedSource[] }) {
  const failed = sources.filter((source) => source.error);
  return (
    <div className="mb-7 rounded-2xl border border-line bg-forest/[0.025] p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <p className="eyebrow">Source check</p>
          <p className="mt-1 text-sm font-bold text-ink">
            {sources.length} source{sources.length === 1 ? "" : "s"} checked
          </p>
        </div>
        <Badge tone={failed.length ? "warning" : "success"}>
          {failed.length
            ? `${failed.length} need attention`
            : "Ready to process"}
        </Badge>
      </div>
      <div className="space-y-2">
        {sources.slice(0, 8).map((source, index) => (
          <div
            key={`${source.canonical}-${index}`}
            className="flex items-center gap-2 text-xs"
          >
            {source.thumbnail ? (
              <img
                src={source.thumbnail}
                alt=""
                crossOrigin="use-credentials"
                className="h-9 w-14 shrink-0 rounded-lg object-cover"
              />
            ) : (
              <span
                className={cn(
                  "h-2 w-2 shrink-0 rounded-full",
                  source.error ? "bg-[#c55b5b]" : "bg-[#3d9d67]",
                )}
              />
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate font-semibold text-ink">
                {source.title || source.input}
              </p>
              <p
                className={cn(
                  "truncate",
                  source.error ? "text-[#a84141]" : "text-muted",
                )}
              >
                {source.error ||
                  [
                    source.channel,
                    source.duration ? `${Math.round(source.duration)}s` : null,
                    source.playlist_count ? `${source.playlist_count} playlist items` : null,
                    source.kind,
                  ]
                    .filter(Boolean)
                    .join(" · ")}
              </p>
            </div>
          </div>
        ))}
        {sources.length > 8 ? (
          <p className="pt-1 text-xs text-muted">
            + {sources.length - 8} more source
            {sources.length - 8 === 1 ? "" : "s"}
          </p>
        ) : null}
      </div>
    </div>
  );
}

function SectionHeader({
  icon: Icon,
  title,
  text,
}: {
  icon: LucideIcon;
  title: string;
  text: string;
}) {
  return (
    <div className="flex gap-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-mint text-forest">
        <Icon size={18} />
      </div>
      <div>
        <h2 className="text-lg font-black tracking-[-0.03em] text-ink">
          {title}
        </h2>
        <p className="mt-1 text-xs leading-5 text-muted">{text}</p>
      </div>
    </div>
  );
}

function FormatChoice({
  active,
  onClick,
  icon: Icon,
  title,
  text,
}: {
  active: boolean;
  onClick: () => void;
  icon: LucideIcon;
  title: string;
  text: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "flex items-center gap-3 rounded-2xl border p-3 text-left transition",
        active
          ? "border-forest bg-mint/30"
          : "border-line bg-panel hover:border-forest/25",
      )}
    >
      <span
        className={cn(
          "flex h-9 w-9 items-center justify-center rounded-xl",
          active ? "bg-forest text-white" : "bg-forest/5 text-muted",
        )}
      >
        <Icon size={16} />
      </span>
      <span>
        <span className="block text-sm font-bold text-ink">{title}</span>
        <span className="block text-xs text-muted">{text}</span>
      </span>
      {active ? <Check size={15} className="ml-auto text-forest" /> : null}
    </button>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4 text-sm font-semibold text-ink">
      <span>{label}</span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative h-6 w-11 rounded-full transition",
          checked ? "bg-forest" : "bg-forest/20",
        )}
      >
        <span
          className={cn(
            "absolute top-1 h-4 w-4 rounded-full bg-white shadow transition",
            checked ? "left-6" : "left-1",
          )}
        />
      </button>
    </div>
  );
}

function CheckField({
  checked,
  onChange,
  children,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-3 text-sm leading-6 text-ink">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        className="mt-1 h-4 w-4 shrink-0 accent-[#143c2c]"
      />
      {children}
    </label>
  );
}

function InlineError({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="mt-4 flex items-start gap-2 rounded-2xl border border-danger-line bg-danger-surface p-3 text-sm leading-5 text-[#a84141]"
    >
      <X size={16} className="mt-0.5 shrink-0" />
      {message}
    </div>
  );
}

function Notice({
  message,
  onClose,
}: {
  message: string;
  onClose: () => void;
}) {
  return (
    <div
      role="status"
      className="mb-6 flex items-start gap-3 rounded-2xl border border-[#ead49b] bg-[#fff9e8] p-4 text-sm leading-6 text-[#755a18]"
    >
      <AlertCircle size={17} className="mt-1 shrink-0" />
      <span className="flex-1">{message}</span>
      <button
        type="button"
        className="btn-ghost !h-8 !w-8 !p-0"
        onClick={onClose}
        aria-label="Dismiss message"
      >
        <X size={15} />
      </button>
    </div>
  );
}

function ExportSummary({
  request,
  className,
  heading = "Export summary",
}: {
  request: ExportRequest;
  className?: string;
  heading?: string;
}) {
  const formats = request.formats.length
    ? request.formats.join(" + ").toUpperCase()
    : "No format selected";
  return (
    <div
      className={cn(
        "rounded-2xl border border-line bg-forest/[0.025] p-4",
        className,
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="eyebrow">{heading}</p>
          <p className="mt-1 text-sm font-bold text-ink">
            {request.images_only ? "Images only" : "Annotated export"}
          </p>
        </div>
        <Badge tone={request.images_only ? "success" : "neutral"}>
          {formats}
        </Badge>
      </div>
      <p className="mt-3 text-xs leading-5 text-muted">
        {request.images_only
          ? "One image per page or slide with no added text: no cover, contents, labels, footer, timestamps, page numbers, or OCR. PDF pages match each image's aspect ratio with no margins."
          : `Annotated output using the ${request.layout} layout, ${request.page_size || "selected"} pages, ${request.orientation || "selected"} orientation, margins, and text options.`}
      </p>
      <p className="mt-2 text-xs leading-5 text-muted">
        {request.images_only
          ? `Title is used for the filename only: ${request.title}`
          : `Filename: ${request.title}`}
      </p>
    </div>
  );
}

function Processing({
  job,
  mode,
  error,
  connectionBanner,
  exportSnapshot,
  onCancel,
  onBack,
  pending,
}: {
  job: JobRecord;
  mode: ProcessingMode;
  error: string | null;
  connectionBanner: string | null;
  exportSnapshot: ExportRequest | null;
  onCancel: () => void;
  onBack: () => void;
  pending: boolean;
}) {
  const isExport = mode === "export";
  return (
    <section className="mx-auto max-w-5xl pb-16">
      <div className="mb-9">
        <p className="eyebrow">Step 3 · {isExport ? "Export" : "Processing"}</p>
        <h1 className="mt-2 text-4xl font-black tracking-[-0.06em] text-ink sm:text-5xl">
          {isExport
            ? exportSnapshot?.images_only
              ? "Freezing your images."
              : "Freezing your notes."
            : "Extracting the useful frames."}
        </h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-muted">
          {isExport
            ? "Your reviewed selection is being rendered. The export uses the frozen options from the review action."
            : "Worker status is live from the backend. No document is generated until you finish review."}
        </p>
      </div>
      <div className="panel p-5 sm:p-8">
        <div className="flex flex-wrap items-start justify-between gap-5">
          <div className="flex items-center gap-4">
            <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-forest text-lime">
              <Loader2
                size={25}
                className={
                  job.status === "running" ||
                  job.status === "queued" ||
                  job.status === "cancelling" ||
                  job.status === "exporting"
                    ? "animate-spin"
                    : ""
                }
              />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-xl font-black text-ink">
                  {humanStatus(job.stage)}
                </h2>
                <Badge tone={statusTone(job.status)}>
                  {humanStatus(job.status)}
                </Badge>
              </div>
              <p className="mt-1 text-sm text-muted">
                Job {job.id.slice(0, 12)} · {job.request.sources.length} source
                {job.request.sources.length === 1 ? "" : "s"}
              </p>
            </div>
          </div>
          <div className="text-right">
            <p className="text-4xl font-black tracking-[-0.07em] text-ink">
              {Math.round(job.progress)}
              <span className="text-xl text-muted">%</span>
            </p>
            <p className="text-xs font-semibold text-muted">
              reported progress
            </p>
          </div>
        </div>
        <div className="mt-8">
          <ProgressBar value={job.progress} className="h-3" />
          <div className="mt-3 flex flex-wrap justify-between gap-2 text-xs font-semibold text-muted">
            <span>{job.stage}</span>
            <span>
              {job.retained_count} retained · {job.duplicate_count} removed
            </span>
          </div>
        </div>
        <div className="mt-8 grid gap-3 sm:grid-cols-3">
          <Metric
            icon={Video}
            label="Sources"
            value={`${job.sources.filter((source) => source.status === "completed").length}/${job.sources.length || job.request.sources.length}`}
          />
          <Metric
            icon={FileImage}
            label="Candidates"
            value={String(job.frame_count)}
          />
          <Metric
            icon={Sparkles}
            label="Retained"
            value={String(job.retained_count)}
          />
        </div>
        {isExport && exportSnapshot ? (
          <ExportSummary
            request={exportSnapshot}
            className="mt-8"
            heading="Export snapshot"
          />
        ) : null}
        {job.sources.length ? (
          <div className="mt-8 border-t border-line pt-6">
            <div className="mb-3 flex items-center justify-between">
              <p className="text-xs font-bold uppercase tracking-[0.14em] text-muted">
                Source progress
              </p>
              <p className="text-xs text-muted">Actual worker metrics</p>
            </div>
            <div className="space-y-2">
              {job.sources.map((source) => (
                <SourceProgress key={source.id} source={source} />
              ))}
            </div>
          </div>
        ) : null}
        {!isExport && (job.techniques || []).length ? (
          <div className="mt-8 border-t border-line pt-6">
            <p className="text-xs font-bold uppercase tracking-[0.14em] text-muted">
              Enabled techniques
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              {(job.techniques || []).map((technique) => (
                <Badge key={technique}>{technique}</Badge>
              ))}
            </div>
          </div>
        ) : null}
        {connectionBanner ? (
          <div className="mt-6 flex gap-2 rounded-2xl border border-[#ead49b] bg-[#fff9e8] p-3 text-sm text-[#755a18]">
            <AlertCircle size={16} className="mt-0.5 shrink-0" />
            {connectionBanner}
          </div>
        ) : null}
        {error ? <InlineError message={error} /> : null}
        <div className="mt-8 flex justify-end gap-3">
          {job.status === "failed" || job.status === "cancelled" ? (
            <button type="button" className="btn-secondary" onClick={onBack}>
              <ArrowLeft size={16} />{" "}
              {isExport ? "Back to review" : "Adjust settings"}
            </button>
          ) : (
            <button
              type="button"
              className="btn-secondary"
              disabled={pending || job.status === "cancelling"}
              onClick={onCancel}
            >
              <StopCircle size={16} />{" "}
              {pending
                ? "Cancelling…"
                : isExport
                  ? "Cancel export"
                  : "Cancel job"}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}

function SourceProgress({ source }: { source: SourceRecord }) {
  const total = source.frame_count || 0;
  const retained = source.retained_count || 0;
  const removed = Math.max(0, total - retained);
  const exact = source.exact_removed || 0;
  const near = source.near_removed || 0;
  return (
    <div className="rounded-xl bg-forest/[0.03] px-3 py-3">
      <div className="flex items-center gap-3">
        <span
          className={cn(
            "h-2 w-2 rounded-full",
            source.status === "completed"
              ? "bg-[#3d9d67]"
              : source.status === "failed"
                ? "bg-[#c55b5b]"
                : source.status === "running"
                  ? "animate-pulse bg-forest"
                  : "bg-forest/15",
          )}
        />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold text-ink">
          {source.title || source.input}
        </span>
        <span className="text-xs text-muted">{humanStatus(source.status)}</span>
      </div>
      {source.status === "completed" ? (
        <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 pl-5 text-[11px] text-muted">
          <span>{total} candidates</span>
          <span>{exact} exact removed</span>
          <span>{near} near removed</span>
          <span>{retained} retained</span>
          <span>
            {removed} removed ({total ? Math.round((removed / total) * 100) : 0}
            %)
          </span>
          <span>{(source.processing_seconds || 0).toFixed(1)}s</span>
        </div>
      ) : null}
    </div>
  );
}

function Metric({
  icon: Icon,
  label,
  value,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
}) {
  return (
    <div className="rounded-2xl border border-line bg-panel p-4">
      <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-[0.12em] text-muted">
        <Icon size={14} />
        {label}
      </div>
      <p className="mt-2 text-2xl font-black tracking-[-0.05em] text-ink">
        {value}
      </p>
    </div>
  );
}

function Review({
  job,
  frames,
  selected,
  setSelected,
  onToggle,
  onBulk,
  onMove,
  draft,
  setDraft,
  onExport,
  onBack,
  error,
  pending,
}: {
  job: JobRecord;
  frames: FrameMetadata[];
  selected: Set<string>;
  setSelected: React.Dispatch<React.SetStateAction<Set<string>>>;
  onToggle: (frame: FrameMetadata) => void;
  onBulk: (retained: boolean) => void;
  onMove: (frameId: string, direction: "up" | "down") => void;
  draft: DraftConfig;
  setDraft: React.Dispatch<React.SetStateAction<DraftConfig>>;
  onExport: (payload: ExportRequest) => void;
  onBack: () => void;
  error: string | null;
  pending: string | null;
}) {
  const [tab, setTab] = useState<ReviewTab>("retained");
  const [source, setSource] = useState("all");
  const [search, setSearch] = useState("");
  const [preview, setPreview] = useState<FrameMetadata | null>(null);
  const previewReturnRef = useRef<HTMLButtonElement | null>(null);
  const [displayLimit, setDisplayLimit] = useState(120);
  const [openSources, setOpenSources] = useState<Set<string>>(
    () => new Set(job.sources.map((item) => item.id)),
  );
  const sourceMap = useMemo(
    () => new Map(job.sources.map((item) => [item.id, item])),
    [job.sources],
  );
  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return sortFrameList(frames, job.sources).filter((frame) => {
      const isSuspected = Boolean(frame.duplicate_of || frame.duplicate_reason);
      const tabMatch =
        tab === "retained"
          ? frame.retained && !isSuspected
          : tab === "suspected"
            ? isSuspected
            : !frame.retained && !isSuspected;
      const sourceMatch = source === "all" || frame.source_id === source;
      const searchMatch =
        !needle ||
        `${frame.source_title} ${frame.ocr_text || ""} ${sourceMap.get(frame.source_id)?.input || ""}`
          .toLowerCase()
          .includes(needle);
      return tabMatch && sourceMatch && searchMatch;
    });
  }, [frames, job.sources, search, source, sourceMap, tab]);
  const visible = filtered.slice(0, displayLimit);
  const exportPayload = buildExportRequest(draft);
  const selectAllVisible = () =>
    setSelected((current) => {
      const next = new Set(current);
      if (visible.length && visible.every((frame) => next.has(frame.id)))
        visible.forEach((frame) => next.delete(frame.id));
      else visible.forEach((frame) => next.add(frame.id));
      return next;
    });
  const groups = useMemo(() => {
    const result: Array<{ source: SourceRecord; frames: FrameMetadata[] }> = [];
    for (const sourceRecord of job.sources) {
      const sourceFrames = visible.filter(
        (frame) => frame.source_id === sourceRecord.id,
      );
      if (sourceFrames.length)
        result.push({ source: sourceRecord, frames: sourceFrames });
    }
    const unknown = visible.filter((frame) => !sourceMap.has(frame.source_id));
    if (unknown.length)
      result.push({
        source: {
          id: "unknown",
          input: "",
          canonical: "",
          kind: "remote",
          title: "Other frames",
          status: "completed",
          frame_count: unknown.length,
        },
        frames: unknown,
      });
    return result;
  }, [job.sources, sourceMap, visible]);

  return (
    <section className="pb-16">
      <div className="mb-7 flex flex-wrap items-end justify-between gap-5">
        <div>
          <p className="eyebrow">Step 4 · Review</p>
          <h1 className="mt-2 text-4xl font-black tracking-[-0.06em] text-ink sm:text-5xl">
            Make it yours.
          </h1>
          <p className="mt-3 max-w-xl text-sm leading-6 text-muted">
            Review retained, suspected, and discarded frames. Edits are saved
            immediately and clear older export artifacts.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            className="btn-secondary !px-4"
            onClick={onBack}
            disabled={Boolean(pending)}
          >
            <ArrowLeft size={15} /> Settings
          </button>
          <button
            type="button"
            className="btn-primary !px-4"
            disabled={
              Boolean(pending) || !draft.formats.length || !job.retained_count
            }
            onClick={() => onExport(exportPayload)}
          >
            <Download size={15} /> Export {job.retained_count}{" "}
            {draft.imagesOnly ? "images" : "frames"}
          </button>
        </div>
      </div>
      <div className="mb-6 grid gap-3 sm:grid-cols-4">
        <Metric
          icon={FileImage}
          label="Candidates"
          value={String(job.frame_count)}
        />
        <Metric
          icon={Sparkles}
          label="Retained"
          value={String(job.retained_count)}
        />
        <Metric
          icon={Filter}
          label="Removed"
          value={String(frames.filter((frame) => !frame.retained).length)}
        />
        <Metric
          icon={ShieldCheck}
          label="Method"
          value={job.request.dedupe_mode}
        />
      </div>
      <SourceStats sources={job.sources} />
      <ExportSummary request={exportPayload} className="mt-6" />
      <div className="panel mt-6 p-4 sm:p-6">
        <div className="flex flex-wrap items-center gap-3 border-b border-line pb-5">
          <div
            className="flex flex-wrap rounded-full bg-forest/5 p-1"
            role="tablist"
            aria-label="Frame status"
          >
            <ReviewTabButton
              active={tab === "retained"}
              onClick={() => {
                setTab("retained");
                setDisplayLimit(120);
              }}
            >
              Retained{" "}
              <span>
                {
                  frames.filter(
                    (frame) => frame.retained && !frame.duplicate_of,
                  ).length
                }
              </span>
            </ReviewTabButton>
            <ReviewTabButton
              active={tab === "suspected"}
              onClick={() => {
                setTab("suspected");
                setDisplayLimit(120);
              }}
            >
              Suspected{" "}
              <span>
                {
                  frames.filter(
                    (frame) => frame.duplicate_of || frame.duplicate_reason,
                  ).length
                }
              </span>
            </ReviewTabButton>
            <ReviewTabButton
              active={tab === "discarded"}
              onClick={() => {
                setTab("discarded");
                setDisplayLimit(120);
              }}
            >
              Discarded{" "}
              <span>
                {
                  frames.filter(
                    (frame) =>
                      !frame.retained &&
                      !frame.duplicate_of &&
                      !frame.duplicate_reason,
                  ).length
                }
              </span>
            </ReviewTabButton>
          </div>
          <div className="relative min-w-[200px] flex-1 sm:max-w-xs">
            <Search
              size={15}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-muted"
            />
            <input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setDisplayLimit(120);
              }}
              className="input-control !rounded-full !py-2 !pl-9"
              placeholder="Search source or OCR"
              aria-label="Search frames"
            />
          </div>
          <SelectControl
            label=""
            value={source}
            onChange={(value) => {
              setSource(value);
              setDisplayLimit(120);
            }}
            options={[
              { value: "all", label: "All sources" },
              ...job.sources.map((item) => ({
                value: item.id,
                label: item.title || item.input,
              })),
            ]}
          />
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3 py-4">
          <div className="flex items-center gap-3">
            <button
              type="button"
              className="btn-ghost !px-0"
              onClick={selectAllVisible}
              disabled={!visible.length || Boolean(pending)}
            >
              <span
                className={cn(
                  "flex h-5 w-5 items-center justify-center rounded border",
                  visible.length &&
                    visible.every((frame) => selected.has(frame.id))
                    ? "border-forest bg-forest text-white"
                    : "border-line bg-panel text-transparent",
                )}
              >
                <Check size={13} />
              </span>
              Select visible
            </button>
            {selected.size ? (
              <span className="chip">{selected.size} selected</span>
            ) : null}
          </div>
          {selected.size ? (
            <div className="flex gap-2">
              <button
                type="button"
                className="btn-ghost !text-[#a84141]"
                onClick={() => onBulk(false)}
                disabled={Boolean(pending)}
              >
                Discard selected
              </button>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => onBulk(true)}
                disabled={Boolean(pending)}
              >
                Restore selected
              </button>
            </div>
          ) : (
            <span className="text-xs text-muted">
              {filtered.length} frame{filtered.length === 1 ? "" : "s"} match ·
              actions stay visible on touch
            </span>
          )}
        </div>
        {groups.length ? (
          <div className="space-y-5">
            {groups.map(({ source: sourceRecord, frames: sourceFrames }) => {
              const isOpen = openSources.has(sourceRecord.id);
              return (
                <section
                  key={sourceRecord.id}
                  className="rounded-2xl border border-line p-3 sm:p-4"
                >
                  <button
                    type="button"
                    className="flex w-full items-center gap-3 text-left"
                    aria-expanded={isOpen}
                    onClick={() =>
                      setOpenSources((current) => {
                        const next = new Set(current);
                        next.has(sourceRecord.id)
                          ? next.delete(sourceRecord.id)
                          : next.add(sourceRecord.id);
                        return next;
                      })
                    }
                  >
                    <ChevronRight
                      size={17}
                      className={cn("transition", isOpen && "rotate-90")}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-bold text-ink">
                        {sourceRecord.title || sourceRecord.input}
                      </span>
                      <span className="mt-1 block text-xs text-muted">
                        {sourceFrames.length} shown · {sourceRecord.kind}
                      </span>
                    </span>
                    <Badge tone="neutral">{sourceRecord.status}</Badge>
                  </button>
                  {isOpen ? (
                    <div className="mt-4 grid gap-4 border-t border-line pt-4 sm:grid-cols-2 xl:grid-cols-3">
                      {sourceFrames.map((frame) => (
                        <FrameCard
                          key={frame.id}
                          jobId={job.id}
                          frame={frame}
                          sourceUrl={sourceRecord.input}
                          selected={selected.has(frame.id)}
                          onSelect={() =>
                            setSelected((current) => {
                              const next = new Set(current);
                              next.has(frame.id)
                                ? next.delete(frame.id)
                                : next.add(frame.id);
                              return next;
                            })
                          }
                          onToggle={() => onToggle(frame)}
                          onPreview={() => {
                            previewReturnRef.current =
                              document.activeElement as HTMLButtonElement | null;
                            setPreview(frame);
                          }}
                          onMove={(direction) => onMove(frame.id, direction)}
                        />
                      ))}
                    </div>
                  ) : null}
                </section>
              );
            })}
          </div>
        ) : (
          <EmptyFrames tab={tab} search={search} />
        )}
        {visible.length < filtered.length ? (
          <button
            type="button"
            className="btn-secondary mx-auto mt-6"
            onClick={() => setDisplayLimit((current) => current + 120)}
          >
            Load more frames <ChevronDown size={15} />
          </button>
        ) : null}
      </div>
      {error ? <InlineError message={error} /> : null}
      <div className="mt-5 flex flex-wrap items-center justify-between gap-3 text-xs text-muted">
        <span className="flex items-center gap-2">
          <ShieldCheck size={14} /> Manual edits are saved immediately.
        </span>
        <span>
          {(job.techniques || []).length
            ? `Enabled: ${(job.techniques || []).join(" · ")}`
            : "No technique metadata reported"}
        </span>
      </div>
      {preview ? (
        <PreviewModal
          jobId={job.id}
          frame={preview}
          sourceUrl={sourceMap.get(preview.source_id)?.input}
          onClose={() => {
            setPreview(null);
            window.requestAnimationFrame(() =>
              previewReturnRef.current?.focus(),
            );
          }}
        />
      ) : null}
    </section>
  );
}

function ReviewTabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "rounded-full px-3 py-2 text-xs font-bold",
        active ? "bg-panel text-ink shadow-sm" : "text-muted",
      )}
    >
      {children}
    </button>
  );
}

function SourceStats({ sources }: { sources: SourceRecord[] }) {
  if (!sources.length) return null;
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {sources.map((source) => {
        const total = source.frame_count || 0;
        const retained = source.retained_count || 0;
        const removed = Math.max(0, total - retained);
        return (
          <div
            key={source.id}
            className="rounded-2xl border border-line bg-panel p-4"
          >
            <div className="flex items-start gap-3">
              <Video size={16} className="mt-1 text-forest" />
              <div className="min-w-0">
                <p className="truncate text-sm font-bold text-ink">
                  {source.title || source.input}
                </p>
                <p className="mt-1 text-xs text-muted">
                  {(source.processing_seconds || 0).toFixed(1)}s processing
                </p>
              </div>
            </div>
            <div className="mt-3 grid grid-cols-4 gap-2 text-center text-xs">
              <div>
                <p className="font-black text-ink">
                  {source.exact_removed || 0}
                </p>
                <p className="text-muted">exact</p>
              </div>
              <div>
                <p className="font-black text-ink">
                  {source.near_removed || 0}
                </p>
                <p className="text-muted">near</p>
              </div>
              <div>
                <p className="font-black text-ink">{retained}</p>
                <p className="text-muted">retained</p>
              </div>
              <div>
                <p className="font-black text-ink">
                  {removed ? `${Math.round((removed / total) * 100)}%` : "0%"}
                </p>
                <p className="text-muted">removed</p>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function EmptyFrames({ tab, search }: { tab: ReviewTab; search: string }) {
  return (
    <div className="flex min-h-[260px] flex-col items-center justify-center rounded-2xl border border-dashed border-line text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-forest/5 text-muted">
        {search ? <Search size={21} /> : <FileImage size={21} />}
      </div>
      <p className="mt-4 text-sm font-bold text-ink">
        {search
          ? "No frames match that search"
          : tab === "retained"
            ? "No retained frames"
            : tab === "suspected"
              ? "No suspected duplicates"
              : "No manually discarded frames"}
      </p>
      <p className="mt-1 max-w-xs text-xs leading-5 text-muted">
        {search
          ? "Try a different title, source, or OCR term."
          : "The worker result will appear here when available."}
      </p>
    </div>
  );
}

function PreviewModal({
  jobId,
  frame,
  sourceUrl,
  onClose,
}: {
  jobId: string;
  frame: FrameMetadata;
  sourceUrl?: string | null;
  onClose: () => void;
}) {
  const image = frameImageUrl(jobId, frame.image_url || frame.thumbnail_url);
  const timestampLink =
    sourceUrl &&
    /^https:\/\/(?:www\.)?(?:youtube\.com|youtu\.be)\//i.test(sourceUrl)
      ? (() => {
          try {
            const url = new URL(sourceUrl);
            url.searchParams.set(
              "t",
              String(Math.max(0, Math.round(frame.timestamp))),
            );
            return url.toString();
          } catch {
            return null;
          }
        })()
      : null;
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent aria-describedby="frame-preview-description">
        <DialogTitle>
          Frame {frame.index} · {frame.source_title}
        </DialogTitle>
        <DialogDescription id="frame-preview-description">
          Expanded frame preview. Press Escape to close and return to the
          preview action.
        </DialogDescription>
        {image ? (
          <img
            src={image}
            crossOrigin="use-credentials"
            alt={`Expanded extracted frame at ${seconds(frame.timestamp)} from ${frame.source_title}`}
            className="mt-5 max-h-[68vh] w-full rounded-2xl bg-frame object-contain"
          />
        ) : (
          <div className="mt-5 flex min-h-[300px] items-center justify-center rounded-2xl bg-frame text-sm text-muted">
            Preview unavailable
          </div>
        )}
        <div className="mt-4 flex flex-wrap justify-between gap-3 text-sm text-muted">
          <span>
            Timestamp{" "}
            {timestampLink ? (
              <a
                href={timestampLink}
                target="_blank"
                rel="noreferrer"
                className="font-semibold text-ink underline underline-offset-2"
              >
                {seconds(frame.timestamp)}{" "}
                <ExternalLink size={13} className="inline" />
              </a>
            ) : (
              seconds(frame.timestamp)
            )}
          </span>
          {frame.ocr_text ? (
            <span className="max-w-xl">{frame.ocr_text}</span>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Results({
  job,
  downloads,
  exportRequest,
  onNew,
  onReview,
  onRefresh,
}: {
  job: JobRecord;
  downloads: DownloadArtifact[];
  exportRequest: ExportRequest | null;
  onNew: () => void;
  onReview: () => void;
  onRefresh: () => Promise<void>;
}) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const warnings = job.warnings || [];
  const techniques = job.techniques || [];

  const refresh = async () => {
    setLoading(true);
    try {
      const [exports] = await Promise.all([getExports(job.id), onRefresh()]);
      setError(
        exports.exports.length || job.downloads.length
          ? null
          : "The export is complete but no download artifacts are listed yet. Refresh again in a moment.",
      );
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Could not refresh export details.",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="mx-auto max-w-5xl pb-16">
      <div className="mb-9 text-center">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-3xl bg-forest text-lime shadow-lg shadow-forest/15">
          <Check size={31} strokeWidth={3} />
        </div>
        <p className="eyebrow mt-6">Step 5 · Export complete</p>
        <h1 className="mt-2 text-4xl font-black tracking-[-0.06em] text-ink sm:text-5xl">
          Your actual artifacts are ready.
        </h1>
        <p className="mx-auto mt-3 max-w-xl text-sm leading-6 text-muted">
          Download the formats returned by the backend, or return to review for
          another frozen snapshot.
        </p>
      </div>
      <div className="grid gap-6 lg:grid-cols-[1fr_.85fr]">
        <div className="panel p-5 sm:p-7">
          <div className="flex items-center justify-between">
            <div>
              <p className="eyebrow">Downloads</p>
              <h2 className="mt-2 text-xl font-black text-ink">
                {downloads.length} file{downloads.length === 1 ? "" : "s"}{" "}
                returned
              </h2>
            </div>
            <button
              type="button"
              className="btn-ghost !h-9 !w-9 !p-0"
              onClick={refresh}
              disabled={loading}
              title="Refresh export metadata"
              aria-label="Refresh export metadata"
            >
              <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
            </button>
          </div>
          <div className="mt-6 space-y-3">
            {downloads.map((item) => (
              <a
                key={item.filename}
                href={
                  item.download_url?.startsWith("http")
                    ? item.download_url
                    : downloadUrl(job.id, item.filename)
                }
                target="_blank"
                rel="noreferrer"
                className="group flex items-center gap-4 rounded-2xl border border-line bg-panel p-4 transition hover:-translate-y-0.5 hover:border-forest/30 hover:shadow-soft"
              >
                <div
                  className={cn(
                    "flex h-11 w-11 items-center justify-center rounded-xl",
                    item.format === "pdf"
                      ? "bg-[#fff0ef] text-[#bb5852]"
                      : "bg-[#fff8df] text-[#a87a1b]",
                  )}
                >
                  {item.format === "pdf" ? (
                    <FileText size={20} />
                  ) : (
                    <Layers3 size={20} />
                  )}
                </div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-bold text-ink">
                    {item.filename}
                  </p>
                  <p className="mt-1 text-xs uppercase tracking-[0.12em] text-muted">
                    {item.format} · {formatBytes(item.size_bytes)}
                    {item.page_count
                      ? ` · ${item.page_count} ${item.format === "pdf" ? "pages" : "slides"}`
                      : ""}
                  </p>
                </div>
                <Download
                  size={17}
                  className="text-muted transition group-hover:text-ink"
                />
              </a>
            ))}
            {!downloads.length ? (
              <div className="rounded-2xl border border-dashed border-line p-5 text-sm text-muted">
                No download artifacts are listed. Use refresh to check the real
                export state.
              </div>
            ) : null}
          </div>
          {error ? <InlineError message={error} /> : null}
        </div>
        <div className="space-y-6">
          {exportRequest ? <ExportSummary request={exportRequest} /> : null}
          <div className="panel p-5 sm:p-7">
            <p className="eyebrow">Processing summary</p>
            <div className="mt-5 grid grid-cols-2 gap-3">
              <Metric
                icon={Video}
                label="Sources"
                value={String(job.sources.length)}
              />
              <Metric
                icon={FileImage}
                label="Candidates"
                value={String(job.frame_count)}
              />
              <Metric
                icon={Sparkles}
                label="Retained"
                value={String(job.retained_count)}
              />
              <Metric
                icon={Filter}
                label="Removed"
                value={String(job.duplicate_count)}
              />
            </div>
            {warnings.length ? (
              <div className="mt-5 rounded-2xl border border-[#ead49b] bg-[#fff9e8] p-4 text-xs leading-5 text-[#755a18]">
                <p className="font-bold">Partial-result warnings</p>
                <ul className="mt-2 list-disc space-y-1 pl-4">
                  {warnings.map((warning) => (
                    <li key={warning}>{warning}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="mt-5 rounded-2xl border border-line bg-forest/[0.025] p-4 text-xs leading-5 text-muted">
              {job.processing_seconds
                ? `Processing took ${job.processing_seconds.toFixed(1)} seconds.`
                : "Processing time was not reported."}
              {techniques.length
                ? ` Enabled techniques: ${techniques.join(", ")}.`
                : ""}
            </div>
          </div>
          <div className="flex flex-wrap justify-end gap-3">
            <button type="button" className="btn-secondary" onClick={onReview}>
              <ArrowLeft size={15} /> Review frames
            </button>
            <button type="button" className="btn-primary" onClick={onNew}>
              <Plus size={15} /> Create another
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
