/* eslint-disable @next/next/no-img-element */
import {
  ArrowDown,
  ArrowUp,
  Check,
  ExternalLink,
  Eye,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { frameImageUrl } from "@/lib/api";
import type { FrameMetadata } from "@/lib/types";
import { Badge, cn } from "./ui";

function timestamp(value: number) {
  const total = Math.max(0, Math.round(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function FrameCard({
  jobId,
  frame,
  sourceUrl,
  selected,
  onSelect,
  onToggle,
  onPreview,
  onMove,
}: {
  jobId: string;
  frame: FrameMetadata;
  sourceUrl?: string | null;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
  onPreview: () => void;
  onMove?: (direction: "up" | "down") => void;
}) {
  const image = frameImageUrl(jobId, frame.thumbnail_url || frame.image_url);
  const duplicate = Boolean(frame.duplicate_of);
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
    <article
      className={cn(
        "group overflow-hidden rounded-2xl border bg-panel transition",
        frame.retained
          ? "border-line hover:-translate-y-0.5 hover:shadow-soft"
          : "border-dashed border-danger-line bg-danger-surface",
      )}
    >
      <div className="relative aspect-video overflow-hidden bg-frame">
        {image ? (
          <img
            src={image}
            crossOrigin="use-credentials"
            alt={`Frame at ${timestamp(frame.timestamp)} from ${frame.source_title}`}
            className="h-full w-full object-contain"
            loading="lazy"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-muted">
            Preview unavailable
          </div>
        )}
        <div className="absolute left-3 top-3 flex gap-1.5">
          <Badge tone={frame.retained ? "success" : "danger"}>
            {frame.retained ? "Included" : "Discarded"}
          </Badge>
          {duplicate ? (
            <Badge tone="warning">
              {frame.duplicate_reason === "exact_hash"
                ? "Exact match"
                : "Near match"}
            </Badge>
          ) : null}
        </div>
        <div className="absolute bottom-3 left-3 rounded-md bg-black/80 px-2 py-1 font-mono text-[11px] font-bold text-white">
          {timestampLink ? (
            <a
              href={timestampLink}
              target="_blank"
              rel="noreferrer"
              className="underline decoration-white/40 underline-offset-2 hover:decoration-white"
            >
              {timestamp(frame.timestamp)}
            </a>
          ) : (
            timestamp(frame.timestamp)
          )}
        </div>
        <div className="absolute right-3 top-3 flex gap-1.5">
          <button
            type="button"
            title="Preview frame"
            aria-label="Preview frame"
            onClick={onPreview}
            className="action-button"
          >
            <Eye size={15} />
          </button>
          <button
            type="button"
            title={frame.retained ? "Discard frame" : "Restore frame"}
            aria-label={frame.retained ? "Discard frame" : "Restore frame"}
            onClick={onToggle}
            className="action-button"
          >
            {frame.retained ? <Trash2 size={15} /> : <RotateCcw size={15} />}
          </button>
          {onMove ? (
            <>
              <button
                type="button"
                title="Move frame up"
                aria-label="Move frame up"
                onClick={() => onMove("up")}
                className="action-button"
              >
                <ArrowUp size={15} />
              </button>
              <button
                type="button"
                title="Move frame down"
                aria-label="Move frame down"
                onClick={() => onMove("down")}
                className="action-button"
              >
                <ArrowDown size={15} />
              </button>
            </>
          ) : null}
        </div>
        <button
          type="button"
          aria-label={selected ? "Deselect frame" : "Select frame"}
          aria-pressed={selected}
          onClick={onSelect}
          className={cn(
            "absolute bottom-3 right-3 flex h-8 w-8 items-center justify-center rounded-full border-2 transition",
            selected
              ? "border-forest bg-forest text-white"
              : "border-white bg-panel/90 text-transparent hover:text-ink",
          )}
        >
          <Check size={14} strokeWidth={3} />
        </button>
      </div>
      <div className="space-y-2 p-3.5">
        <div className="flex items-start justify-between gap-2">
          <p className="line-clamp-2 text-sm font-bold leading-5 text-ink">
            {frame.source_title}
          </p>
          <span className="shrink-0 rounded bg-forest/5 px-1.5 py-1 font-mono text-[10px] text-muted">
            #{frame.index}
          </span>
        </div>
        {frame.ocr_text ? (
          <p className="line-clamp-2 text-xs leading-5 text-muted">
            {frame.ocr_text}
          </p>
        ) : (
          <p className="text-xs text-muted">No OCR text captured</p>
        )}
        {duplicate && frame.matched_frame_id ? (
          <p className="flex items-center gap-1 text-[11px] text-warning">
            <ExternalLink size={11} />
            Matches an earlier frame
          </p>
        ) : null}
      </div>
    </article>
  );
}
