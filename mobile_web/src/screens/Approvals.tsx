import { useCallback, useState } from "react";
import { api, ApiError } from "../api";
import type { ConnectionConfig, Interrupt } from "../types";
import { ErrorBanner, Spinner, formatWhen, usePolling } from "../components/ui";

interface Props {
  config: ConnectionConfig;
  onOpenRun: (runId: number) => void;
  onPendingCount: (count: number) => void;
}

export function ApprovalsScreen({ config, onOpenRun, onPendingCount }: Props) {
  const [items, setItems] = useState<Interrupt[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const pending = await api.interrupts(config);
      setItems(pending);
      onPendingCount(pending.length);
      setError(null);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [config, onPendingCount]);

  usePolling(() => void load(), 8000);

  const answer = async (interrupt: Interrupt) => {
    const value = (answers[interrupt.id] ?? "").trim();
    if (!value) return;
    setBusyId(interrupt.id);
    setError(null);
    try {
      await api.answerInterrupt(config, interrupt.id, value, true);
      setItems((prev) => prev.filter((item) => item.id !== interrupt.id));
      setAnswers((prev) => {
        const next = { ...prev };
        delete next[interrupt.id];
        return next;
      });
      onPendingCount(Math.max(0, items.length - 1));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="screen">
      <div className="row between">
        <span className="muted">
          {items.length > 0 ? `${items.length} approvazioni in attesa` : "Nessuna approvazione"}
        </span>
        <button className="btn small ghost" onClick={() => void load()} disabled={loading}>
          {loading ? <Spinner /> : "Aggiorna"}
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        {!loading && items.length === 0 ? (
          <div className="empty">
            Quando un workflow si ferma per una conferma, la trovi qui.
          </div>
        ) : null}

        {items.map((interrupt) => (
          <div className="card" key={interrupt.id}>
            <div className="row between">
              <h3>Run #{interrupt.run_id}</h3>
              <span className="chip waiting_human">{interrupt.node_id || "HITL"}</span>
            </div>
            <p className="muted">{interrupt.question || "Confermi il passaggio?"}</p>
            <p className="muted">{formatWhen(interrupt.created_at)}</p>

            {(interrupt.options ?? []).length > 0 ? (
              <div className="actions">
                {(interrupt.options ?? []).map((option) => (
                  <button
                    key={option}
                    className="btn small"
                    onClick={() => setAnswers((prev) => ({ ...prev, [interrupt.id]: option }))}
                    style={
                      answers[interrupt.id] === option
                        ? { borderColor: "#38bdf8", background: "rgba(56,189,248,0.15)" }
                        : undefined
                    }
                  >
                    {option}
                  </button>
                ))}
              </div>
            ) : null}

            <div className="field">
              <label htmlFor={`answer-${interrupt.id}`}>Risposta / feedback</label>
              <textarea
                id={`answer-${interrupt.id}`}
                value={answers[interrupt.id] ?? ""}
                onChange={(event) =>
                  setAnswers((prev) => ({ ...prev, [interrupt.id]: event.target.value }))
                }
                placeholder="Scegli un'opzione o scrivi un feedback…"
              />
            </div>

            <div className="actions">
              <button
                className="btn primary"
                onClick={() => void answer(interrupt)}
                disabled={busyId === interrupt.id || !(answers[interrupt.id] ?? "").trim()}
              >
                {busyId === interrupt.id ? "Invio…" : "Invia e riprendi"}
              </button>
              <button className="btn ghost" onClick={() => onOpenRun(interrupt.run_id)}>
                Apri run
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
