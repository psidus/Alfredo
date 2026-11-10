import type {
  ChatMessage,
  ChatReply,
  ConnectionConfig,
  Decision,
  FileListing,
  FilePreview,
  Interrupt,
  InterruptOutcome,
  RunDetail,
  RunEventPage,
  RunFile,
  RunStart,
  RunSummary,
  ShareLink,
  SystemStatus,
  UploadChunkStatus,
  UploadSession,
  WorkflowCard,
} from "./types";

export class ApiError extends Error {
  readonly status?: number;
  readonly detail?: unknown;

  constructor(message: string, status?: number, detail?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function normalizeBaseUrl(baseUrl: string): string {
  return (baseUrl || "").trim().replace(/\/+$/, "");
}

function errorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object") {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (Array.isArray(detail)) {
      const parts = detail
        .map((item) =>
          item && typeof item === "object"
            ? String((item as { msg?: unknown }).msg ?? "")
            : String(item),
        )
        .filter(Boolean);
      if (parts.length) return parts.join(" · ");
    }
  }
  return fallback;
}

async function request<T>(
  config: ConnectionConfig,
  method: "GET" | "POST" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const base = normalizeBaseUrl(config.baseUrl);
  const headers: Record<string, string> = {
    Accept: "application/json",
    "ngrok-skip-browser-warning": "true",
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (config.token) headers["X-Control-Token"] = config.token;

  let response: Response;
  try {
    response = await fetch(`${base}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    throw new ApiError(
      `Impossibile raggiungere il server (${base || "URL mancante"}). Verifica la rete e le impostazioni.`,
      undefined,
      error,
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text.slice(0, 500) };
    }
  }

  if (!response.ok) {
    const message =
      response.status === 401
        ? "Token non valido: controlla ALFREDO_CONTROL_TOKEN nelle impostazioni."
        : errorMessage(payload, `Richiesta fallita (HTTP ${response.status})`);
    throw new ApiError(message, response.status, payload);
  }

  return payload as T;
}

export const api = {
  health: (config: ConnectionConfig) =>
    request<{ status: string; version?: string }>(config, "GET", "/api/health"),

  systemStatus: (config: ConnectionConfig) =>
    request<SystemStatus>(config, "GET", "/api/system/status"),

  downloadFile: async (config: ConnectionConfig, path: string): Promise<Blob> => {
    const base = normalizeBaseUrl(config.baseUrl);
    const headers: Record<string, string> = { Accept: "*/*" };
    if (config.token) headers["X-Control-Token"] = config.token;
    let response: Response;
    try {
      response = await fetch(
        `${base}/api/files/download?path=${encodeURIComponent(path)}`,
        { headers },
      );
    } catch (error) {
      throw new ApiError("Impossibile scaricare il file: rete non raggiungibile.", undefined, error);
    }
    if (!response.ok) {
      let detail: unknown = null;
      try {
        detail = await response.json();
      } catch {
        detail = null;
      }
      throw new ApiError(
        errorMessage(detail, `Download fallito (HTTP ${response.status})`),
        response.status,
        detail,
      );
    }
    return response.blob();
  },

  workflows: (config: ConnectionConfig) =>
    request<WorkflowCard[]>(config, "GET", "/api/workflows"),

  chat: (config: ConnectionConfig, message: string, history: ChatMessage[], autoRoute = true) =>
    request<ChatReply>(config, "POST", "/api/assistant/chat", {
      message,
      history: history
        .filter((m) => m.role === "user" || m.role === "assistant")
        .map((m) => ({ role: m.role, content: m.content })),
      auto_route: autoRoute,
    }),

  decide: (config: ConnectionConfig, message: string, history: ChatMessage[]) =>
    request<Decision>(config, "POST", "/api/assistant/decide", {
      message,
      history: history.map((m) => ({ role: m.role, content: m.content })),
    }),

  startRun: (
    config: ConnectionConfig,
    workflowId: number,
    inputs: Record<string, unknown> = {},
  ) =>
    request<RunStart>(config, "POST", "/api/runs", {
      workflow_id: workflowId,
      inputs,
      source: "mobile-web",
    }),

  runs: (config: ConnectionConfig, limit = 20) =>
    request<RunSummary[]>(config, "GET", `/api/runs?limit=${Math.max(1, Math.min(100, limit))}`),

  run: (config: ConnectionConfig, runId: number) =>
    request<RunDetail>(config, "GET", `/api/runs/${runId}`),

  cancelRun: (config: ConnectionConfig, runId: number) =>
    request<{ run_id: number; status: string }>(config, "POST", `/api/runs/${runId}/cancel`),

  deleteRun: (config: ConnectionConfig, runId: number) =>
    request<{ run_id: number; status: string }>(config, "DELETE", `/api/runs/${runId}`),

  runEvents: (config: ConnectionConfig, runId: number, afterSeq = 0, limit = 200) =>
    request<RunEventPage>(
      config,
      "GET",
      `/api/runs/${runId}/events?after_seq=${Math.max(0, afterSeq)}&limit=${Math.max(
        1,
        Math.min(1000, limit),
      )}`,
    ),

  runFiles: async (config: ConnectionConfig, runId: number): Promise<RunFile[]> => {
    const payload = await request<{ files?: RunFile[] }>(
      config,
      "GET",
      `/api/runs/${runId}/files`,
    );
    return payload.files ?? [];
  },

  filePreview: (config: ConnectionConfig, path: string) =>
    request<FilePreview>(
      config,
      "GET",
      `/api/files/preview?path=${encodeURIComponent(path)}`,
    ),

  listFiles: (config: ConnectionConfig, path = "") =>
    request<FileListing>(
      config,
      "GET",
      `/api/files?path=${encodeURIComponent(path)}`,
    ),

  deleteFile: (config: ConnectionConfig, path: string) =>
    request<{ status: string; path: string; is_dir: boolean }>(
      config,
      "DELETE",
      `/api/files?path=${encodeURIComponent(path)}`,
    ),

  startUpload: (
    config: ConnectionConfig,
    path: string,
    size: number,
    chunkSize = 1024 * 1024,
  ) =>
    request<UploadSession>(config, "POST", "/api/files/upload", {
      path,
      size,
      chunk_size: chunkSize,
    }),

  uploadChunk: async (
    config: ConnectionConfig,
    uploadId: string,
    index: number,
    chunk: Blob,
  ): Promise<UploadChunkStatus> => {
    const base = normalizeBaseUrl(config.baseUrl);
    const headers: Record<string, string> = {
      Accept: "application/json",
      "Content-Type": "application/octet-stream",
      "ngrok-skip-browser-warning": "true",
    };
    if (config.token) headers["X-Control-Token"] = config.token;
    let response: Response;
    try {
      response = await fetch(
        `${base}/api/files/upload/${encodeURIComponent(uploadId)}/chunks/${index}`,
        { method: "PUT", headers, body: chunk },
      );
    } catch (error) {
      throw new ApiError("Impossibile inviare il chunk: rete non raggiungibile.", undefined, error);
    }
    const text = await response.text();
    let payload: unknown = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text.slice(0, 500) };
      }
    }
    if (!response.ok) {
      throw new ApiError(
        errorMessage(payload, `Caricamento fallito (HTTP ${response.status})`),
        response.status,
        payload,
      );
    }
    return payload as UploadChunkStatus;
  },

  completeUpload: (config: ConnectionConfig, uploadId: string, sha256?: string) =>
    request<RunFile>(
      config,
      "POST",
      `/api/files/upload/${encodeURIComponent(uploadId)}/complete`,
      sha256 ? { sha256 } : {},
    ),

  abortUpload: (config: ConnectionConfig, uploadId: string) =>
    request<{ upload_id: string; aborted: boolean }>(
      config,
      "DELETE",
      `/api/files/upload/${encodeURIComponent(uploadId)}`,
    ),

  shareFile: (config: ConnectionConfig, path: string, ttlSeconds = 3600) =>
    request<ShareLink>(config, "POST", "/api/files/share", {
      path,
      ttl_seconds: ttlSeconds,
    }),

  interrupts: (config: ConnectionConfig, runId?: number) =>
    request<Interrupt[]>(
      config,
      "GET",
      runId == null ? "/api/interrupts" : `/api/interrupts?run_id=${runId}`,
    ),

  answerInterrupt: (
    config: ConnectionConfig,
    interruptId: number,
    answer: string,
    execute = true,
  ) =>
    request<InterruptOutcome>(config, "POST", `/api/interrupts/${interruptId}/answer`, {
      answer,
      execute,
    }),

  pushKey: (config: ConnectionConfig) =>
    request<{ public_key: string; enabled: boolean; subject?: string }>(
      config,
      "GET",
      "/api/push/key",
    ),

  pushSubscribe: (
    config: ConnectionConfig,
    subscription: {
      endpoint: string;
      keys: { p256dh: string; auth: string };
      user_agent?: string;
    },
  ) =>
    request<{ status: string; subscription_id?: number; subscriptions?: number }>(
      config,
      "POST",
      "/api/push/subscribe",
      subscription,
    ),

  pushUnsubscribe: (config: ConnectionConfig, endpoint: string) =>
    request<{ status: string; subscriptions?: number }>(
      config,
      "POST",
      "/api/push/unsubscribe",
      { endpoint },
    ),

  pushTest: (config: ConnectionConfig) =>
    request<{ status: string; delivered: number }>(config, "POST", "/api/push/test"),
};
