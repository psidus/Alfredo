import { useEffect, useRef } from "react";

export function StatusChip({ status }: { status: string }) {
  const normalized = (status || "unknown").toLowerCase();
  const label =
    normalized === "waiting_human"
      ? "attende te"
      : normalized === "completed"
        ? "completato"
        : normalized === "failed"
          ? "errore"
          : normalized === "running"
            ? "in corso"
            : normalized === "paused"
              ? "in pausa"
              : normalized === "cancelled" || normalized === "canceled"
                ? "annullato"
                : normalized;
  return <span className={`chip ${normalized}`}>{label}</span>;
}

export function ErrorBanner({
  message,
  onDismiss,
}: {
  message: string | null;
  onDismiss?: () => void;
}) {
  if (!message) return null;
  return (
    <div className="error" role="alert">
      {message}
      {onDismiss ? (
        <div className="actions">
          <button className="btn small ghost" onClick={onDismiss}>
            Chiudi
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" aria-label="caricamento" />;
}

export function TypingDots() {
  return (
    <span className="typing" aria-label="Alfredo sta scrivendo">
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

export function formatWhen(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const diffMs = Date.now() - date.getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "adesso";
  if (minutes < 60) return `${minutes} min fa`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h fa`;
  return date.toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });
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
