import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { clearChat, loadChat, saveChat } from "../storage";
import type { ChatMessage, ConnectionConfig, WorkflowSuggestion } from "../types";
import { ErrorBanner, TypingDots, usePolling } from "../components/ui";
import { VoicePlayerButton, VoiceRecorderButton } from "../components/VoiceControls";
import { useLanguage } from "../i18n";

interface Props {
  config: ConnectionConfig;
  onStarted: (runId: number, label: string) => void;
  onMasterModel?: (model: string | null) => void;
}

function suggestionKeys(suggestion: WorkflowSuggestion): string[] {
  const keys = new Set<string>();
  for (const key of Object.keys(suggestion.inputs ?? {})) keys.add(key);
  for (const key of suggestion.missing_inputs ?? []) keys.add(key);
  return [...keys];
}

function SuggestionCard({
  config,
  suggestion,
  onStarted,
}: {
  config: ConnectionConfig;
  suggestion: WorkflowSuggestion;
  onStarted: (runId: number, label: string) => void;
}) {
  const { t } = useLanguage();
  const keys = useMemo(() => suggestionKeys(suggestion), [suggestion]);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const [key, value] of Object.entries(suggestion.inputs ?? {})) {
      initial[key] = value == null ? "" : String(value);
    }
    return initial;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const missing = (suggestion.missing_inputs ?? []).filter(
    (key) => !(values[key] ?? "").trim(),
  );

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const inputs: Record<string, unknown> = {};
      for (const [key, value] of Object.entries(values)) {
        if (value.trim()) inputs[key] = value.trim();
      }
      const run = await api.startRun(config, suggestion.workflow_id, inputs);
      onStarted(run.run_id, suggestion.name || `Workflow #${suggestion.workflow_id}`);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="suggestion">
      <div className="row between">
        <strong>{suggestion.name || `Workflow #${suggestion.workflow_id}`}</strong>
        <span className="chip running">{t("proposed")}</span>
      </div>
      {suggestion.text ? <div className="muted">{suggestion.text}</div> : null}

      {keys.map((key) => (
        <div className="field" key={key}>
          <label htmlFor={`sugg-${suggestion.workflow_id}-${key}`}>{key}</label>
          <input
            id={`sugg-${suggestion.workflow_id}-${key}`}
            value={values[key] ?? ""}
            placeholder={t("requiredValue")}
            onChange={(event) => setValues((prev) => ({ ...prev, [key]: event.target.value }))}
          />
        </div>
      ))}

      <ErrorBanner message={error} />

      <div className="actions">
        <button className="btn primary" onClick={start} disabled={busy || missing.length > 0}>
          {busy ? t("starting") : t("startWorkflow")}
        </button>
        {missing.length > 0 ? (
          <span className="muted">
            {t("missingFields")}: {missing.join(", ")}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export function ChatScreen({ config, onStarted, onMasterModel }: Props) {
  const { lang, t } = useLanguage();
  const [messages, setMessages] = useState<ChatMessage[]>(() => loadChat());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [voiceActive, setVoiceActive] = useState(false);
  const [autoPlayIndex, setAutoPlayIndex] = useState<number | null>(null);
  const [autoVoice, setAutoVoice] = useState<boolean>(() => {
    try {
      return localStorage.getItem("alfredo_auto_voice") === "true";
    } catch {
      return false;
    }
  });
  const [activeRunIds, setActiveRunIds] = useState<number[]>(() => {
    try {
      const stored = localStorage.getItem("alfredo_active_runs");
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });
  const [queue, setQueue] = useState<{
    waiting: number;
    behind: number | null;
    eta: number | null;
  } | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem("alfredo_active_runs", JSON.stringify(activeRunIds));
    } catch {
      // Ignore storage errors
    }
  }, [activeRunIds]);

  useEffect(() => {
    void api.runs(config, 5).then((recent) => {
      const running = recent.filter((r) => r.status === "running").map((r) => r.run_id);
      if (running.length > 0) {
        setActiveRunIds((prev) => [...new Set([...prev, ...running])]);
      }
    }).catch(() => {});
  }, [config]);

  usePolling(
    async () => {
      if (activeRunIds.length === 0) return;
      for (const runId of activeRunIds) {
        try {
          const detail = await api.run(config, runId);
          if (detail.status === "completed" || detail.status === "failed") {
            setActiveRunIds((prev) => prev.filter((id) => id !== runId));
            const isOk = detail.status === "completed";
            const icon = isOk ? "✅" : "⚠️";
            const title = isOk
              ? (lang === "en" ? `Run #${runId} completed!` : `L'esecuzione #${runId} è stata completata con successo!`)
              : (lang === "en" ? `Run #${runId} failed.` : `L'esecuzione #${runId} è terminata con errore.`);

            let summary = "";
            if (detail.result) {
              try {
                const parsed = JSON.parse(detail.result);
                summary = parsed.final_result || parsed.action_plan || detail.result;
              } catch {
                summary = detail.result;
              }
            }
            if (summary.length > 400) {
              summary = summary.slice(0, 400) + "...";
            }

            const content = `${icon} **${title}**\n\n${summary}`.trim();
            setMessages((prev) => [
              ...prev,
              {
                role: "assistant",
                content,
                started_run_id: runId,
              },
            ]);
          }
        } catch {
          // Ignore network glitch
        }
      }
    },
    4000,
    activeRunIds.length > 0,
  );

  const refreshQueue = useCallback(async () => {
    try {
      const status = await api.systemStatus(config);
      const modelName = status.master_model || status.loaded_model || null;
      if (modelName) {
        onMasterModel?.(modelName);
      }
      const first = status.queue?.waiting?.[0];
      setQueue({
        waiting: status.queue?.length ?? 0,
        behind: first?.run_id ?? status.queue?.current?.run_id ?? null,
        eta: first?.eta_s ?? null,
      });
    } catch {
      // Informational only: never block the chat on the status endpoint.
    }
  }, [config, onMasterModel]);

  usePolling(() => void refreshQueue(), 10000, true);

  useEffect(() => {
    try {
      localStorage.setItem("alfredo_auto_voice", String(autoVoice));
    } catch {
      // Ignore storage errors
    }
  }, [autoVoice]);

  useEffect(() => {
    saveChat(messages);
    const node = logRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, busy]);

  const sendText = async (
    rawText: string,
    opts?: { isVoice?: boolean; voiceDuration?: number },
  ) => {
    const text = rawText.trim();
    if (!text || busy) return;
    const history = messages;
    const next: ChatMessage[] = [
      ...messages,
      {
        role: "user",
        content: text,
        isVoice: opts?.isVoice,
        voiceDuration: opts?.voiceDuration,
      },
    ];
    const shouldAutoSpeak = Boolean(opts?.isVoice || autoVoice);
    setMessages(next);
    if (!opts?.isVoice) {
      setDraft("");
    }
    setBusy(true);
    setError(null);
    try {
      const reply = await api.chat(config, text, history);
      if (reply.model) {
        onMasterModel?.(reply.model);
      }
      if (reply.started_run_id) {
        setActiveRunIds((prev) =>
          prev.includes(reply.started_run_id!) ? prev : [...prev, reply.started_run_id!],
        );
      }
      const replyIndex = next.length;
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: reply.reply || t("noTextReply"),
          workflow: reply.workflow ?? null,
          started_run_id: reply.started_run_id ?? null,
        },
      ]);
      if (shouldAutoSpeak && (reply.reply || "").trim()) {
        setAutoPlayIndex(replyIndex);
      }
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const send = () => sendText(draft);

  return (
    <div className="screen">
      <div className="row between wrap">
        <span className="muted">{t("chatHeaderHint")}</span>
        <div className="row" style={{ gap: "6px" }}>
          <button
            type="button"
            className={`btn small ${autoVoice ? "primary" : "ghost"}`}
            onClick={() => setAutoVoice((prev) => !prev)}
          >
            {autoVoice ? t("voiceAutoOn") : t("voiceAutoOff")}
          </button>
          <button
            className="btn small ghost"
            onClick={() => {
              clearChat();
              setMessages([]);
              setAutoPlayIndex(null);
              setError(null);
            }}
            disabled={messages.length === 0 || busy}
          >
            {t("newChat")}
          </button>
        </div>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      {queue && queue.waiting > 0 ? (
        <div className="banner">
          {t("gpuBusy")}: {queue.waiting} {t("inQueue")}
          {queue.behind ? ` · ${t("behindRun")} #${queue.behind}` : ""}
          {queue.eta != null ? ` · ~${Math.round(queue.eta)} s` : ""}
        </div>
      ) : null}

      <div className="chat-log" ref={logRef}>
        {messages.length === 0 ? (
          <div className="empty">
            {t("chatEmptyPrompt")}
            <br />
            {t("chatEmptyExample")}
          </div>
        ) : null}

        {messages.map((message, index) =>
          message.role === "assistant" && message.workflow ? (
            <div className="assistant-block" key={`${message.role}-${index}`}>
              <div className="bubble assistant">
                {message.content}
                {message.started_run_id ? (
                  <div style={{ marginTop: "0.5rem" }}>
                    <button
                      className="btn small primary"
                      onClick={() => onStarted(message.started_run_id!, `Run #${message.started_run_id}`)}
                      style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                    >
                      <span>🚀</span>
                      <span>{t("openRun")} #{message.started_run_id}</span>
                    </button>
                  </div>
                ) : null}
                <VoicePlayerButton
                  config={config}
                  text={message.content}
                  autoPlay={autoPlayIndex === index}
                  onAutoPlayed={() => setAutoPlayIndex(null)}
                  onError={setError}
                />
              </div>
              <SuggestionCard
                config={config}
                suggestion={message.workflow}
                onStarted={(runId, label) => {
                  setActiveRunIds((prev) => (prev.includes(runId) ? prev : [...prev, runId]));
                  onStarted(runId, label);
                }}
              />
            </div>
          ) : (
            <div key={`${message.role}-${index}`} className={`bubble ${message.role}`}>
              {message.isVoice ? (
                <div className="voice-badge">
                  {t("voiceBadge")}
                  {message.voiceDuration ? ` · ${Math.round(message.voiceDuration)}s` : ""}
                </div>
              ) : null}
              {message.content}
              {message.started_run_id ? (
                <div style={{ marginTop: "0.5rem" }}>
                  <button
                    className="btn small primary"
                    onClick={() => onStarted(message.started_run_id!, `Run #${message.started_run_id}`)}
                    style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}
                  >
                    <span>🚀</span>
                    <span>{t("openRun")} #{message.started_run_id}</span>
                  </button>
                </div>
              ) : null}
              {message.role === "assistant" ? (
                <VoicePlayerButton
                  config={config}
                  text={message.content}
                  autoPlay={autoPlayIndex === index}
                  onAutoPlayed={() => setAutoPlayIndex(null)}
                  onError={setError}
                />
              ) : null}
            </div>
          ),
        )}

        {busy ? (
          <div className="bubble assistant">
            <TypingDots />
          </div>
        ) : null}
      </div>

      <div className="chat-input">
        {!voiceActive ? (
          <textarea
            rows={1}
            value={draft}
            placeholder={t("chatInputPlaceholder")}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
        ) : null}
        <VoiceRecorderButton
          config={config}
          disabled={busy}
          onActiveChange={setVoiceActive}
          onTranscribeToDraft={(text) =>
            setDraft((prev) => (prev.trim() ? `${prev.trim()} ${text}` : text))
          }
          onSendDirect={(text, durationS) =>
            void sendText(text, { isVoice: true, voiceDuration: durationS })
          }
          onError={setError}
        />
        {!voiceActive ? (
          <button className="send" onClick={() => void send()} disabled={busy || !draft.trim()}>
            {t("send")}
          </button>
        ) : null}
      </div>
    </div>
  );
}
