import type {
  ExportListResponse,
  ExportRequest,
  ExportResponse,
  AnalyzeResponse,
  FrameListResponse,
  JobRecord,
  JobRequest,
  JobSummary,
  JobsResponse,
  ProgressEvent,
} from "./types";

const SERVER_API_BASE = "http://localhost:8000";

export interface BrowserLocationLike {
  protocol: string;
  hostname: string;
}

function normalizeHostname(hostname: string): string {
  return hostname
    .trim()
    .toLowerCase()
    .replace(/^\[|\]$/g, "")
    .replace(/\.+$/, "");
}

function formatHostname(hostname: string): string {
  return hostname.includes(":") ? `[${hostname}]` : hostname;
}

function isLoopbackHostname(hostname: string): boolean {
  const normalized = normalizeHostname(hostname);
  if (normalized === "localhost" || normalized === "::1") return true;
  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.slice(1).every((octet) => {
      const value = Number(octet);
      return /^\d+$/.test(octet) && value >= 0 && value <= 255;
    })
  );
}

function browserApiBase(location: BrowserLocationLike): string | null {
  const protocol = location.protocol.toLowerCase();
  if (protocol !== "http:" && protocol !== "https:") return null;
  const hostname = normalizeHostname(location.hostname);
  if (!hostname) return null;
  return `${protocol}//${formatHostname(hostname)}:8000`;
}

function parseConfiguredApiBase(configuredBase: string): URL | null {
  try {
    const parsed = new URL(configuredBase);
    if (
      (parsed.protocol !== "http:" && parsed.protocol !== "https:") ||
      !parsed.hostname ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash
    )
      return null;
    return parsed;
  } catch {
    return null;
  }
}

/** Resolve browser defaults while preserving intentional non-loopback config. */
export function resolveApiBaseUrl(
  configuredBase: string | undefined,
  location?: BrowserLocationLike | null,
): string {
  const browserLocation =
    location === undefined
      ? typeof window === "undefined"
        ? null
        : {
            protocol: window.location.protocol,
            hostname: window.location.hostname,
          }
      : location;
  const configured = configuredBase?.trim();
  const parsed = configured ? parseConfiguredApiBase(configured) : null;
  const browserBase = browserLocation ? browserApiBase(browserLocation) : null;

  if (!parsed) return browserBase || SERVER_API_BASE;

  if (
    browserLocation &&
    isLoopbackHostname(parsed.hostname) &&
    isLoopbackHostname(browserLocation.hostname)
  ) {
    parsed.hostname = normalizeHostname(browserLocation.hostname);
  }

  return parsed.toString().replace(/\/+$/, "");
}

function currentApiBase(): string {
  return resolveApiBaseUrl(process.env.NEXT_PUBLIC_API_BASE_URL);
}

export const API_BASE = currentApiBase();

export function apiUrl(path: string): string {
  const base = currentApiBase();
  return `${base}${path.startsWith("/") ? path : `/${path}`}`;
}

function apiUnavailableError(): ApiError {
  const origin =
    typeof window === "undefined"
      ? "the current server"
      : `browser origin (${window.location.origin})`;
  return new ApiError(
    `The FrameNotes API at ${currentApiBase()} is not reachable from ${origin}. Check the API and network connection, then try again.`,
    0,
  );
}

class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

let sessionPromise: Promise<void> | null = null;

/** Establish the cookie session once. Every subsequent request is same-session. */
export function initializeSession(): Promise<void> {
  if (typeof window === "undefined") return Promise.resolve();
  if (!sessionPromise) {
    sessionPromise = fetch(apiUrl("/api/v1/session"), {
      method: "GET",
      credentials: "include",
      headers: { Accept: "application/json" },
    })
      .then(async (response) => {
        if (!response.ok)
          throw new ApiError(
            "Could not initialize the FrameNotes session.",
            response.status,
          );
      })
      .catch((error) => {
        sessionPromise = null;
        throw error;
      });
  }
  return sessionPromise;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    await initializeSession();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw apiUnavailableError();
  }
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      ...init,
      credentials: "include",
      headers: {
        ...((typeof FormData !== "undefined" &&
          init?.body instanceof FormData) ||
        typeof init?.body !== "string"
          ? {}
          : { "Content-Type": "application/json" }),
        ...(init?.headers || {}),
      },
    });
  } catch {
    throw apiUnavailableError();
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as {
        detail?: string | { msg?: string }[];
      };
      if (typeof body.detail === "string") message = body.detail;
      if (Array.isArray(body.detail))
        message = body.detail
          .map((item) => item.msg || "Invalid value")
          .join("; ");
    } catch {
      // Keep the useful HTTP status when the server did not return JSON.
    }
    throw new ApiError(message, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function createJob(payload: JobRequest): Promise<JobRecord> {
  return request<JobRecord>("/api/v1/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function uploadSource(
  file: File,
  config: JobRequest,
): Promise<JobRecord> {
  const query = new URLSearchParams({
    filename: file.name,
    config: JSON.stringify(config),
  });
  return request<JobRecord>(`/api/v1/jobs/upload?${query.toString()}`, {
    method: "POST",
    body: file,
    headers: { "Content-Type": file.type || "application/octet-stream" },
  });
}

export async function getJobs(): Promise<JobsResponse> {
  const response = await request<
    JobsResponse | JobSummary[] | { jobs?: JobSummary[] }
  >("/api/v1/jobs");
  return { jobs: Array.isArray(response) ? response : response.jobs || [] };
}

export function getHealth(): Promise<{ status: string; service?: string }> {
  return request<{ status: string; service?: string }>("/api/v1/health");
}

export function analyzeSources(sources: string[]): Promise<AnalyzeResponse> {
  return request<AnalyzeResponse>("/api/v1/analyze", {
    method: "POST",
    body: JSON.stringify({ sources }),
  });
}

export function getJob(jobId: string): Promise<JobRecord> {
  return request<JobRecord>(`/api/v1/jobs/${encodeURIComponent(jobId)}`);
}

export function cancelJob(jobId: string): Promise<JobRecord> {
  return request<JobRecord>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
}

export function getFrames(
  jobId: string,
  status?: "retained" | "discarded",
  search?: string,
  offset = 0,
  limit = 1000,
): Promise<FrameListResponse> {
  const params = new URLSearchParams({
    limit: String(Math.min(1000, Math.max(1, limit))),
    offset: String(offset),
  });
  if (status) params.set("status", status);
  if (search) params.set("search", search);
  return request<FrameListResponse>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/frames?${params.toString()}`,
  );
}

/** Load the complete review set without allowing an unbounded request. */
export async function getAllFrames(
  jobId: string,
  maxFrames = 6000,
): Promise<FrameListResponse> {
  const frames: FrameListResponse["frames"] = [];
  let offset = 0;
  let total = 0;
  while (frames.length < maxFrames) {
    const page = await getFrames(
      jobId,
      undefined,
      undefined,
      offset,
      Math.min(1000, maxFrames - frames.length),
    );
    frames.push(...page.frames);
    total = page.total;
    if (!page.next_cursor || !page.frames.length) break;
    offset = Number(page.next_cursor);
    if (!Number.isFinite(offset)) break;
  }
  return {
    frames,
    total,
    offset: 0,
    limit: frames.length,
    next_cursor: frames.length < total ? String(frames.length) : null,
  };
}

export function updateFrameRetention(
  jobId: string,
  frameId: string,
  retained: boolean,
): Promise<JobRecord> {
  return request<JobRecord>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/frames/${encodeURIComponent(frameId)}/retention`,
    {
      method: "PATCH",
      body: JSON.stringify({ retained }),
    },
  );
}

export function bulkUpdateRetention(
  jobId: string,
  frameIds: string[],
  retained: boolean,
): Promise<{ job: JobRecord; updated: number }> {
  return request<{ job: JobRecord; updated: number }>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/frames/bulk`,
    {
      method: "POST",
      body: JSON.stringify({
        frame_ids: frameIds,
        status: retained ? "retained" : "discarded",
      }),
    },
  );
}

export function reorderFrames(
  jobId: string,
  frameIds: string[],
): Promise<JobRecord> {
  return request<JobRecord>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/frames/order`,
    {
      method: "PATCH",
      body: JSON.stringify({ frame_ids: frameIds }),
    },
  );
}

export function createExport(
  jobId: string,
  payload: ExportRequest,
): Promise<ExportResponse> {
  return request<ExportResponse>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/export`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function getExports(jobId: string): Promise<ExportListResponse> {
  return request<ExportListResponse>(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/exports`,
  );
}

export function downloadUrl(jobId: string, filename: string): string {
  return apiUrl(
    `/api/v1/jobs/${encodeURIComponent(jobId)}/downloads/${encodeURIComponent(filename)}`,
  );
}

export function frameImageUrl(
  jobId: string,
  imageUrl?: string | null,
): string | null {
  if (!imageUrl) return null;
  return imageUrl.startsWith("http") ? imageUrl : apiUrl(imageUrl);
}

export function subscribeToProgress(
  jobId: string,
  onEvent: (event: ProgressEvent) => void,
  onError?: () => void,
): () => void {
  const source = new EventSource(
    apiUrl(`/api/v1/jobs/${encodeURIComponent(jobId)}/progress`),
    { withCredentials: true },
  );
  const handleMessage = (message: MessageEvent<string>) => {
    try {
      onEvent(JSON.parse(message.data) as ProgressEvent);
    } catch {
      // Ignore malformed event payloads and keep the stream alive.
    }
  };
  source.addEventListener("progress", handleMessage);
  source.onerror = () => onError?.();
  return () => source.close();
}

export function deleteJob(jobId: string): Promise<void> {
  return request<void>(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
    method: "DELETE",
  });
}

export { ApiError };
