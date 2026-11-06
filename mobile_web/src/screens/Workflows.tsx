import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import type { ConnectionConfig, WorkflowCard } from "../types";
import { ErrorBanner, Spinner } from "../components/ui";

interface Props {
  config: ConnectionConfig;
  onStarted: (runId: number, label: string) => void;
}

function WorkflowForm({
  config,
  workflow,
  onStarted,
}: {
  config: ConnectionConfig;
  workflow: WorkflowCard;
  onStarted: (runId: number, label: string) => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const inputs: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(values)) {
        if (value.trim()) inputs[key] = value.trim();
      }
      const run = await api.startRun(config, workflow.id, inputs);
      onStarted(run.run_id, workflow.name);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      {workflow.inputs.map((input) => (
        <div className="field" key={input.key}>
          <label htmlFor={`wf-${workflow.id}-${input.key}`}>{input.key}</label>
          <input
            id={`wf-${workflow.id}-${input.key}`}
            value={values[input.key] ?? ""}
            placeholder={input.prompt || "valore"}
            onChange={(event) =>
              setValues((prev) => ({ ...prev, [input.key]: event.target.value }))
            }
          />
        </div>
      ))}

      {workflow.expected_exports && workflow.expected_exports.length > 0 ? (
        <p className="muted">Output attesi: {workflow.expected_exports.join(", ")}</p>
      ) : null}

      <ErrorBanner message={error} />

      <div className="actions">
        <button className="btn primary" onClick={() => void start()} disabled={busy}>
          {busy ? "Avvio…" : "Avvia"}
        </button>
      </div>
    </div>
  );
}

export function WorkflowsScreen({ config, onStarted }: Props) {
  const [workflows, setWorkflows] = useState<WorkflowCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setWorkflows(await api.workflows(config));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [config]);

  useEffect(() => {
    void load();
  }, [load]);

  const sorted = useMemo(
    () => [...workflows].sort((a, b) => a.name.localeCompare(b.name)),
    [workflows],
  );

  return (
    <div className="screen">
      <div className="row between">
        <span className="muted">
          {workflows.length > 0
            ? `${workflows.length} workflow salvati`
            : "Nessun workflow ancora"}
        </span>
        <button className="btn small ghost" onClick={() => void load()} disabled={loading}>
          {loading ? <Spinner /> : "Aggiorna"}
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        {loading && workflows.length === 0 ? <div className="empty">Carico i workflow…</div> : null}

        {!loading && workflows.length === 0 ? (
          <div className="empty">Crea un workflow dalla dashboard per vederlo qui.</div>
        ) : null}

        {sorted.map((workflow) => {
          const open = openId === workflow.id;
          return (
            <div className="card" key={workflow.id}>
              <button
                className="row between"
                onClick={() => setOpenId(open ? null : workflow.id)}
                style={{ width: "100%", background: "none", border: 0, padding: 0, textAlign: "left" }}
              >
                <span>
                  <h2>{workflow.name}</h2>
                  {workflow.description ? <span className="muted">{workflow.description}</span> : null}
                </span>
                <span className="chip">{workflow.inputs.length} input</span>
              </button>

              {open ? (
                <WorkflowForm
                  config={config}
                  workflow={workflow}
                  onStarted={onStarted}
                />
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
