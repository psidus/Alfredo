import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { clearChat, loadChat, saveChat } from "../storage";
import type { ChatMessage, ConnectionConfig, WorkflowSuggestion } from "../types";
import { ErrorBanner, TypingDots, usePolling } from "../components/ui";
import { useLanguage } from "../i18n";

interface Props {
  config: ConnectionConfig;
  onStarted: (runId: number, label: string) => void;
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

export function ChatScreen({ config, onStarted }: Props) {
  const { t } = useLanguage();
  const [messages, setMessages] = useState<ChatMessage[]>(() => loadChat());
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [queue, setQueue] = useState<{
    waiting: number;
    behind: number | null;
    eta: number | null;
  } | null>(null);
  const logRef = useRef<HTMLDivElement>(null);

  const refreshQueue = useCallback(async () => {
    try {
      const status = await api.systemStatus(config);
      const first = status.queue?.waiting?.[0];
      setQueue({
        waiting: status.queue?.length ?? 0,
        behind: first?.run_id ?? status.queue?.current?.run_id ?? null,
        eta: first?.eta_s ?? null,
      });
    } catch {
      // Informational only: never block the chat on the status endpoint.
    }
  }, [config]);

  usePolling(() => void refreshQueue(), 10000, true);

  useEffect(() => {
    saveChat(messages);
    const node = logRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [messages, busy]);

  const send = async () => {
    const text = draft.trim();
    if (!text || busy) return;
    const history = messages;
    const next: ChatMessage[] = [...messages, { role: "user", content: text }];
    setMessages(next);
    setDraft("");
    setBusy(true);
    setError(null);
    try {
      const reply = await api.chat(config, text, history);
      setMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: reply.reply || t("noTextReply"),
          workflow: reply.workflow ?? null,
        },
      ]);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="screen">
      <div className="row between">
        <span className="muted">{t("chatHeaderHint")}</span>
        <button
          className="btn small ghost"
          onClick={() => {
            clearChat();
            setMessages([]);
            setError(null);
          }}
          disabled={messages.length === 0 || busy}
        >
          {t("newChat")}
        </button>
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
              <div className="bubble assistant">{message.content}</div>
              <SuggestionCard
                config={config}
                suggestion={message.workflow}
                onStarted={onStarted}
              />
            </div>
          ) : (
            <div key={`${message.role}-${index}`} className={`bubble ${message.role}`}>
              {message.content}
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
        <button className="send" onClick={() => void send()} disabled={busy || !draft.trim()}>
          {t("send")}
        </button>
      </div>
    </div>
  );
}
