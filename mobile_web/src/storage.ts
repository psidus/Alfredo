import type { ChatMessage, ConnectionConfig } from "./types";

const CONFIG_KEY = "alfredo.config.v1";
const CHAT_KEY = "alfredo.chat.v1";
const MAX_CHAT_MESSAGES = 40;

function defaultBaseUrl(): string {
  const fromEnv = import.meta.env.VITE_ALFREDO_API_URL as string | undefined;
  if (fromEnv) return fromEnv;
  if (typeof window !== "undefined" && window.location.origin.startsWith("http")) {
    return window.location.origin;
  }
  return "http://127.0.0.1:8000";
}

export function loadConfig(): ConnectionConfig {
  try {
    const raw = localStorage.getItem(CONFIG_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ConnectionConfig>;
      return {
        baseUrl: typeof parsed.baseUrl === "string" && parsed.baseUrl ? parsed.baseUrl : defaultBaseUrl(),
        token: typeof parsed.token === "string" ? parsed.token : "",
      };
    }
  } catch {
    // Corrupted storage: fall back to defaults.
  }
  return { baseUrl: defaultBaseUrl(), token: "" };
}

export function saveConfig(config: ConnectionConfig): void {
  try {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(config));
  } catch {
    // Private mode / quota: config simply won't persist.
  }
}

export function loadChat(): ChatMessage[] {
  try {
    const raw = localStorage.getItem(CHAT_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ChatMessage[];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((m) => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string")
      .slice(-MAX_CHAT_MESSAGES);
  } catch {
    return [];
  }
}

export function saveChat(messages: ChatMessage[]): void {
  try {
    localStorage.setItem(CHAT_KEY, JSON.stringify(messages.slice(-MAX_CHAT_MESSAGES)));
  } catch {
    // ignore
  }
}

export function clearChat(): void {
  try {
    localStorage.removeItem(CHAT_KEY);
  } catch {
    // ignore
  }
}
