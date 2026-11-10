import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type {
  ConnectionConfig,
  FilePreview,
  Interrupt,
  RunDetail,
  RunEvent,
  RunFile,
  ShareLink,
} from "../types";
import { isTerminal, needsApproval } from "../types";
import { ErrorBanner, Spinner, StatusChip, formatBytes, formatWhen, usePolling } from "../components/ui";
import { FilePreviewCard } from "../components/FilePreviewCard";
import { useLanguage, type Language, type TranslationKey } from "../i18n";

interface Props {
  config: ConnectionConfig;
  runId: number;
  onBack: () => void;
  lang?: Language;
}

function getEventLabel(type: string, t: (k: TranslationKey) => string): string {
  switch (type) {
    case "run.started":
      return t("evStarted");
    case "run.paused":
      return t("evPaused");
    case "run.finished":
      return t("evFinished");
    case "run.failed":
      return t("evFailed");
    case "run.cancelled":
      return t("evCancelled");
    case "run.node.started":
      return t("evNodeStarted");
    case "run.node.completed":
      return t("evNodeCompleted");
    case "run.node.failed":
      return t("evNodeFailed");
    case "run.interrupt.opened":
      return t("evInterruptOpened");
    case "run.interrupt.answered":
      return t("evInterruptAnswered");
    case "automation.fired":
      return t("evAutomation");
    default:
      return type;
  }
}

function eventDetail(event: RunEvent): string {
  const payload = event.payload ?? {};
  const name = typeof payload.name === "string" ? payload.name : "";
  const node = typeof payload.node_id === "string" ? payload.node_id : "";
  const question = typeof payload.question === "string" ? payload.question : "";
  if (question) return question;
  if (name) return name;
  if (node) return node;
  if (typeof payload.rule_name === "string") return payload.rule_name;
  return "";
}

export function RunDetailScreen({ config, runId, onBack, lang: propLang }: Props) {
  const context = useLanguage();
  const currentLang = propLang || context.lang || "it";
  const t = context.t;

  const [run, setRun] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [runFiles, setRunFiles] = useState<RunFile[]>([]);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [share, setShare] = useState<ShareLink | null>(null);
  const [interrupt, setInterrupt] = useState<Interrupt | null>(null);
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inFlight = useRef(false);
  const eventsCursor = useRef(0);

  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const detail = await api.run(config, runId);
      setRun(detail);
      setError(null);
      const page = await api.runEvents(config, runId, eventsCursor.current);
      if (page.events.length > 0) {
        eventsCursor.current = page.latest_seq;
        setEvents((previous) => [...previous, ...page.events]);
      }
      const files = await api.runFiles(config, runId).catch(() => []);
      if (files.length > 0) setRunFiles(files);
      if (needsApproval(detail)) {
        const pending = await api.interrupts(config, runId);
        const match =
          pending.find((item) => item.id === detail.pending_interrupt_id) ?? pending[0] ?? null;
        setInterrupt(match);
      } else {
        setInterrupt(null);
      }
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      inFlight.current = false;
    }
  }, [config, runId]);

  useEffect(() => {
    setRun(null);
    setEvents([]);
    setRunFiles([]);
    setPreview(null);
    setShare(null);
    setInterrupt(null);
    setAnswer("");
    setError(null);
    eventsCursor.current = 0;
  }, [runId]);

  const pollEnabled = !run || !isTerminal(run.status);
  usePolling(() => void load(), 3000, pollEnabled);

  const sendAnswer = async () => {
    if (!interrupt || !answer.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.answerInterrupt(config, interrupt.id, answer.trim(), true);
      setInterrupt(null);
      setAnswer("");
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!window.confirm(currentLang === "en" ? "Cancel this run?" : "Annullare questa esecuzione?")) return;
    setBusy(true);
    setError(null);
    try {
      await api.cancelRun(config, runId);
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const handleDeleteRun = async () => {
    if (!window.confirm(t("confirmDeleteRun"))) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteRun(config, runId);
      onBack();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
      setBusy(false);
    }
  };

  const openPreview = async (file: RunFile) => {
    setBusy(true);
    setError(null);
    try {
      setPreview(await api.filePreview(config, file.path));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const download = async (file: RunFile) => {
    setBusy(true);
    setError(null);
    try {
      const blob = await api.downloadFile(config, file.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = file.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const createShare = async (file: RunFile) => {
    setBusy(true);
    setError(null);
    try {
      setShare(await api.shareFile(config, file.path, 24 * 3600));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const finished = run ? isTerminal(run.status) : false;

  return (
    <div className="screen">
      <div className="row between">
        <button className="btn small ghost btn-nav-back" onClick={onBack}>
          <svg
            className="arrow-left-svg"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M10 13L5 8L10 3" />
          </svg>
          <span className="nav-text">{currentLang === "en" ? "Back" : "Indietro"}</span>
        </button>
        <span className="muted">Run #{runId}</span>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        {!run ? (
          <div className="empty row" style={{ justifyContent: "center" }}>
            <Spinner /> {t("loading")}
          </div>
        ) : (
          <>
            <div className="card">
              <div className="row between">
                <h2>{t("status")}</h2>
                <StatusChip status={run.status} lang={currentLang} />
              </div>
              <p className="muted">
                Workflow {run.workflow_id ?? "?"} · {run.source || "?"} ·{" "}
                {formatWhen(run.started_at, currentLang)}
              </p>
              {!finished ? (
                <div className="actions">
                  <button className="btn small danger" onClick={() => void cancel()} disabled={busy}>
                    {t("cancelExecution")}
                  </button>
                </div>
              ) : (
                <div className="actions">
                  <button className="btn small danger" onClick={() => void handleDeleteRun()} disabled={busy}>
                    {t("delete")} {currentLang === "en" ? "Run" : "Esecuzione"}
                  </button>
                </div>
              )}
            </div>

            {events.length > 0 ? (
              <div className="card">
                <div className="row between">
                  <h3>{t("eventsTitle")}</h3>
                  <span className="muted">
                    {events.length} {currentLang === "en" ? "events" : "eventi"}
                  </span>
                </div>
                <ul className="timeline">
                  {events.map((event) => (
                    <li key={`${event.run_id}-${event.seq}`}>
                      <span className="timeline-seq">#{event.seq}</span>
                      <span>{getEventLabel(event.type, t)}</span>
                      {eventDetail(event) ? (
                        <span className="muted"> · {eventDetail(event)}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {interrupt ? (
              <div className="card" style={{ borderColor: "rgba(251,191,36,0.5)" }}>
                <h3>{currentLang === "en" ? "Approval requested" : "Approvazione richiesta"}</h3>
                <p className="muted">{interrupt.question || t("confirmStep")}</p>
                <div className="actions">
                  {(interrupt.options ?? []).map((option) => (
                    <button
                      key={option}
                      className="btn"
                      onClick={() => setAnswer(option)}
                      disabled={busy}
                      style={
                        answer === option
                          ? { borderColor: "#38bdf8", background: "rgba(56,189,248,0.15)" }
                          : undefined
                      }
                    >
                      {option}
                    </button>
                  ))}
                </div>
                <div className="field">
                  <label htmlFor="approval-answer">{t("answerOrFeedback")}</label>
                  <textarea
                    id="approval-answer"
                    value={answer}
                    onChange={(event) => setAnswer(event.target.value)}
                    placeholder={t("answerPlaceholder")}
                  />
                </div>
                <div className="actions">
                  <button
                    className="btn primary"
                    onClick={() => void sendAnswer()}
                    disabled={busy || !answer.trim()}
                  >
                    {busy ? t("sending") : t("sendAndResume")}
                  </button>
                </div>
              </div>
            ) : null}

            {run.result ? (
              <div className="card">
                <h3>{currentLang === "en" ? "Result" : "Risultato"}</h3>
                <div className="code">{run.result}</div>
              </div>
            ) : null}

            {run.procedure_summary ? (
              <div className="card">
                <h3>{currentLang === "en" ? "Procedure Summary" : "Riepilogo procedura"}</h3>
                <div className="muted" style={{ whiteSpace: "pre-wrap" }}>
                  {run.procedure_summary}
                </div>
              </div>
            ) : null}

            {runFiles.length > 0 ? (
              <div className="card">
                <h3>
                  {t("filesExportTitle")} ({runFiles.length})
                </h3>
                {runFiles.map((file) => (
                  <div className="file-row" key={file.path || file.name}>
                    <div className="file-meta">
                      <strong>{file.name}</strong>
                      <span className="muted">
                        {file.kind}
                        {file.size != null ? ` · ${formatBytes(file.size)}` : ""}
                        {file.exists === false
                          ? (currentLang === "en" ? " · not found" : " · non trovato")
                          : ""}
                      </span>
                    </div>
                    {file.path && file.exists !== false ? (
                      <div className="actions">
                        <button
                          className="btn small"
                          onClick={() => void openPreview(file)}
                          disabled={busy}
                        >
                          {t("preview")}
                        </button>
                        <button
                          className="btn small"
                          onClick={() => void download(file)}
                          disabled={busy}
                        >
                          {t("download")}
                        </button>
                        <button
                          className="btn small ghost"
                          onClick={() => void createShare(file)}
                          disabled={busy}
                        >
                          {t("shareLink")}
                        </button>
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : null}

            {preview ? (
              <FilePreviewCard preview={preview} onClose={() => setPreview(null)} />
            ) : null}

            {share ? (
              <div className="card">
                <h3>{currentLang === "en" ? "Shared link" : "Link condiviso"}</h3>
                <p className="muted">
                  {currentLang === "en" ? "Expires at " : "Scade il "}
                  {share.expires_at}
                </p>
                <div className="actions">
                  <a className="btn small" href={share.url} target="_blank" rel="noreferrer">
                    {currentLang === "en" ? "Open link" : "Apri link"}
                  </a>
                  <button
                    className="btn small ghost"
                    onClick={() => {
                      void navigator.clipboard?.writeText(share.url);
                    }}
                  >
                    {currentLang === "en" ? "Copy" : "Copia"}
                  </button>
                  <button className="btn small ghost" onClick={() => setShare(null)}>
                    {t("close")}
                  </button>
                </div>
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
