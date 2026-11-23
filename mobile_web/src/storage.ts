import type { ChatMessage, ConnectionConfig, ContextMode } from "./types";

const CONFIG_KEY = "alfredo.config.v1";
const CHAT_KEY = "alfredo.chat.v1";
const CHAT_SETTINGS_KEY = "alfredo.chat_settings.v1";
const MAX_CHAT_MESSAGES = 40;

export const DEFAULT_ENABLED_MODULES: string[] = [
  "workflows",
  "files_code",
  "memory_kb",
];

export interface ChatSettings {
  enabledModules: string[];
  contextMode: ContextMode;
}

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

export function loadChatSettings(): ChatSettings {
  const fallback: ChatSettings = {
    enabledModules: [...DEFAULT_ENABLED_MODULES],
    contextMode: "auto",
  };
  try {
    const raw = localStorage.getItem(CHAT_SETTINGS_KEY);
    if (!raw) return fallback;
    const parsed = JSON.parse(raw) as Partial<ChatSettings>;
    const enabledModules = Array.isArray(parsed.enabledModules)
      ? parsed.enabledModules.filter((id): id is string => typeof id === "string" && id.trim().length > 0)
      : fallback.enabledModules;
    const contextMode: ContextMode =
      parsed.contextMode === "graphify" || parsed.contextMode === "repomix" || parsed.contextMode === "auto"
        ? parsed.contextMode
        : "auto";
    return { enabledModules, contextMode };
  } catch {
    return fallback;
  }
}

export function saveChatSettings(settings: ChatSettings): void {
  try {
    localStorage.setItem(CHAT_SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // ignore
  }
}
