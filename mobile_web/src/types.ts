/** Payload shapes of the Alfredo Control API (mirrors core/api_models.py). */

export interface WorkflowInput {
  key: string;
  prompt?: string;
}

export interface WorkflowCard {
  id: number;
  name: string;
  description?: string;
  inputs: WorkflowInput[];
  expected_exports?: string[];
}

export interface WorkflowSuggestion {
  workflow_id: number;
  name?: string;
  text?: string;
  inputs?: Record<string, unknown>;
  missing_inputs?: string[];
}

export interface ChatReply {
  reply: string;
  workflow?: WorkflowSuggestion | null;
  started_run_id?: number | null;
}

export type DecisionAction = "answer" | "run_workflow" | "clarify";

export interface Decision {
  text: string;
  action: DecisionAction;
  workflow_id?: number | null;
  inputs?: Record<string, unknown>;
  missing_inputs?: string[];
}

export interface RunStart {
  run_id: number;
  status: string;
}

export interface RunSummary {
  run_id: number;
  workflow_id?: number | null;
  status: string;
  source?: string;
  started_at?: string;
  preview?: string;
}

export interface RunDetail {
  run_id: number;
  workflow_id?: number | null;
  status: string;
  source?: string;
  started_at?: string;
  inputs?: Record<string, unknown>;
  result?: string;
  exports?: string[];
  procedure_summary?: string | null;
  handoffs?: Array<Record<string, unknown>>;
  pending_interrupt_id?: number | null;
}

export interface Interrupt {
  id: number;
  run_id: number;
  node_id?: string;
  question?: string;
  options?: string[];
  created_at?: string;
}

export interface InterruptOutcome {
  status: string;
  run_id?: number;
  execution?: string;
  error?: string;
}

export interface RunEvent {
  id?: number | null;
  run_id: number;
  seq: number;
  type: string;
  payload?: Record<string, unknown>;
  created_at?: string;
}

export interface RunEventPage {
  run_id: number;
  events: RunEvent[];
  latest_seq: number;
}

export interface RunFile {
  name: string;
  path: string;
  size?: number | null;
  kind?: string;
  mime?: string;
  modified?: string;
  is_dir?: boolean;
  accessible?: boolean;
  exists?: boolean;
}

export interface FilePreview {
  name: string;
  path?: string;
  kind: string;
  size?: number | null;
  text?: string | null;
  rows?: string[][];
  sheets?: string[];
  data_uri?: string | null;
  pages?: number | null;
  truncated?: boolean;
  message?: string | null;
  error?: string | null;
}

export interface ShareLink {
  token: string;
  url: string;
  path: string;
  expires_at: string;
  ttl_seconds: number;
}

export interface FileListing {
  path: string;
  entries?: RunFile[];
  truncated?: boolean;
}

export interface UploadSession {
  upload_id: string;
  path: string;
  size: number;
  chunk_size: number;
  chunks: number;
  received?: number[];
  complete?: boolean;
  expires_at: string;
}

export interface UploadChunkStatus {
  upload_id: string;
  index: number;
  received: number[];
  chunks: number;
  complete: boolean;
}

export interface SystemStatus {
  gpu: GpuInfo;
  profiles?: Record<string, GpuProfileStatus>;
  queue: GpuQueueState;
  metrics?: GpuMetrics;
  loaded_model?: string | null;
  preload_error?: string | null;
}

export interface GpuInfo {
  available: boolean;
  name?: string;
  index?: number | null;
  total_mb?: number | null;
  used_mb?: number | null;
  free_mb?: number | null;
  reserve_mb?: number;
  error?: string | null;
}

export interface GpuProfileStatus {
  name: string;
  model: string;
  context_tokens?: number;
  estimated_vram_mb?: number;
  description?: string;
  fits?: boolean | null;
  headroom_mb?: number | null;
  avg_call_s?: number | null;
}

export interface GpuQueueCurrent {
  profile: string;
  model?: string;
  run_id?: number | null;
  started_at?: string | null;
  elapsed_s?: number;
}

export interface GpuQueueWaiting {
  position: number;
  profile: string;
  model?: string;
  run_id?: number | null;
  waiting_s?: number;
  eta_s?: number | null;
}

export interface GpuQueueState {
  current?: GpuQueueCurrent | null;
  waiting?: GpuQueueWaiting[];
  length?: number;
}

export interface GpuFirstToken {
  count: number;
  avg_s?: number | null;
  p95_s?: number | null;
  last_s?: number | null;
}

export interface GpuMetrics {
  served?: number;
  rejected?: number;
  timeouts?: number;
  evictions?: number;
  avg_wait_s?: number | null;
  first_token?: Record<string, GpuFirstToken>;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  workflow?: WorkflowSuggestion | null;
  started_run_id?: number | null;
}

export type Tab = "chat" | "workflows" | "runs" | "approvals" | "files";

export interface ConnectionConfig {
  baseUrl: string;
  token: string;
}

export const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "canceled"];

export function isTerminal(status: string): boolean {
  return TERMINAL_STATUSES.includes(status.toLowerCase());
}

export function needsApproval(run: RunDetail): boolean {
  return run.pending_interrupt_id != null || run.status.toLowerCase() === "waiting_human";
}
