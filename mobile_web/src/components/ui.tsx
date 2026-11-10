import { useEffect, useRef } from "react";
import { useLanguage, type Language } from "../i18n";

export function StatusChip({ status, lang }: { status: string; lang?: Language }) {
  const context = useLanguage();
  const currentLang = lang || context?.lang || "it";
  const normalized = (status || "unknown").toLowerCase();

  let label = normalized;
  if (normalized === "waiting_human") {
    label = currentLang === "en" ? "needs you" : "attende te";
  } else if (normalized === "completed") {
    label = currentLang === "en" ? "completed" : "completato";
  } else if (normalized === "failed") {
    label = currentLang === "en" ? "error" : "errore";
  } else if (normalized === "running") {
    label = currentLang === "en" ? "running" : "in corso";
  } else if (normalized === "paused") {
    label = currentLang === "en" ? "paused" : "in pausa";
  } else if (normalized === "cancelled" || normalized === "canceled") {
    label = currentLang === "en" ? "cancelled" : "annullato";
  }

  return (
    <span
      className={`chip ${normalized}`}
      style={{ whiteSpace: "nowrap", flexShrink: 0 }}
    >
      {label}
    </span>
  );
}

export function ErrorBanner({
  message,
  onDismiss,
}: {
  message: string | null;
  onDismiss?: () => void;
}) {
  const context = useLanguage();
  const closeLabel = context?.t ? context.t("close") : "Chiudi";
  if (!message) return null;
  return (
    <div className="error" role="alert">
      {message}
      {onDismiss ? (
        <div className="actions">
          <button className="btn small ghost" onClick={onDismiss}>
            {closeLabel}
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

export function TypingDots() {
  return (
    <span className="typing" aria-label="typing">
      <span />
      <span />
      <span />
    </span>
  );
}

export function formatBytes(size?: number | null): string {
  if (size == null) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatWhen(value?: string | null, lang?: Language): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const isEn = lang === "en";
  const diffMs = Date.now() - date.getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return isEn ? "just now" : "adesso";
  if (minutes < 60) return isEn ? `${minutes}m ago` : `${minutes} min fa`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return isEn ? `${hours}h ago` : `${hours} h fa`;
  return date.toLocaleString(isEn ? "en-US" : "it-IT", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

/** Poll while mounted; pauses when the tab is hidden to save phone battery. */
export function usePolling(callback: () => void, intervalMs: number, enabled = true) {
  const saved = useRef(callback);
  saved.current = callback;

  useEffect(() => {
    if (!enabled) return;
    const tick = () => {
      if (document.visibilityState === "visible") saved.current();
    };
    tick();
    const id = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(id);
  }, [enabled, intervalMs]);
}
