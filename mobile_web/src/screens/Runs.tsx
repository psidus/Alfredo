import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import type { ConnectionConfig, RunSummary } from "../types";
import { ErrorBanner, Spinner, StatusChip, formatWhen, usePolling } from "../components/ui";

interface Props {
  config: ConnectionConfig;
  onOpen: (runId: number) => void;
}

export function RunsScreen({ config, onOpen }: Props) {
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

  usePolling(() => void load(), 5000);

  return (
    <div className="screen">
      <div className="row between">
        <span className="muted">Aggiornamento automatico ogni 5 s</span>
        <button className="btn small ghost" onClick={() => void load()} disabled={loading}>
          {loading ? <Spinner /> : "Aggiorna"}
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        {loading && runs.length === 0 ? <div className="empty">Carico le esecuzioni…</div> : null}
        {!loading && runs.length === 0 ? (
          <div className="empty">Nessuna esecuzione. Avvia un workflow dalla chat o dalla lista.</div>
        ) : null}

        <div className="list">
          {runs.map((run) => (
            <button className="list-item" key={run.run_id} onClick={() => onOpen(run.run_id)}>
              <div className="row between">
                <strong>Run #{run.run_id}</strong>
                <StatusChip status={run.status} />
              </div>
              <div className="row wrap muted" style={{ gap: 8 }}>
                <span>workflow {run.workflow_id ?? "?"}</span>
                <span>·</span>
                <span>{run.source || "?"}</span>
                <span>·</span>
                <span>{formatWhen(run.started_at)}</span>
              </div>
              {run.preview ? <div className="muted">{run.preview}</div> : null}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
