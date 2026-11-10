import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import type { ConnectionConfig, RunSummary } from "../types";
import { ErrorBanner, Spinner, StatusChip, formatWhen, usePolling } from "../components/ui";
import { useLanguage } from "../i18n";

interface Props {
  config: ConnectionConfig;
  onOpen: (runId: number) => void;
}

export function RunsScreen({ config, onOpen }: Props) {
  const { lang, t } = useLanguage();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRuns(await api.runs(config, 30));
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [config]);

  const handleDeleteRun = async (runId: number) => {
    if (!window.confirm(t("confirmDeleteRun"))) return;
    try {
      await api.deleteRun(config, runId);
      setRuns((prev) => prev.filter((r) => r.run_id !== runId));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    }
  };

  usePolling(() => void load(), 5000);

  return (
    <div className="screen">
      <div className="row between">
        <span className="muted">{t("autoRefreshEvery5s")}</span>
        <button className="btn small ghost" onClick={() => void load()} disabled={loading}>
          {loading ? <Spinner /> : t("refresh")}
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        {loading && runs.length === 0 ? <div className="empty">{t("loadingRuns")}</div> : null}
        {!loading && runs.length === 0 ? (
          <div className="empty">{t("noRunsYet")}</div>
        ) : null}

        <div className="list">
          {runs.map((run) => (
            <button className="list-item" key={run.run_id} onClick={() => onOpen(run.run_id)}>
              <div className="row between">
                <strong>Run #{run.run_id}</strong>
                <div style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <StatusChip status={run.status} lang={lang} />
                  <button
                    className="btn-trash"
                    title={t("delete")}
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDeleteRun(run.run_id);
                    }}
                    aria-label={`Delete Run #${run.run_id}`}
                  >
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      <line x1="10" y1="11" x2="10" y2="17" />
                      <line x1="14" y1="11" x2="14" y2="17" />
                    </svg>
                  </button>
                </div>
              </div>
              <div className="row wrap muted" style={{ gap: 8 }}>
                <span>workflow {run.workflow_id ?? "?"}</span>
                <span>·</span>
                <span>{run.source || "?"}</span>
                <span>·</span>
                <span>{formatWhen(run.started_at, lang)}</span>
              </div>
              {run.preview ? <div className="muted">{run.preview}</div> : null}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
