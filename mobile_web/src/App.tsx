import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { loadConfig, saveConfig } from "./storage";
import type { ConnectionConfig, Tab } from "./types";
import { ChatScreen } from "./screens/Chat";
import { WorkflowsScreen } from "./screens/Workflows";
import { RunsScreen } from "./screens/Runs";
import { RunDetailScreen } from "./screens/RunDetail";
import { ApprovalsScreen } from "./screens/Approvals";
import { SettingsScreen } from "./screens/Settings";
import { FilesScreen } from "./screens/Files";
import { usePolling } from "./components/ui";

type View = "tabs" | "settings" | "run";

const TAB_TITLES: Record<Tab, { title: string; hint: string }> = {
  chat: { title: "Alfredo", hint: "assistente libero" },
  workflows: { title: "Workflow", hint: "avvia un processo salvato" },
  runs: { title: "Esecuzioni", hint: "storico e stato in tempo reale" },
  approvals: { title: "Approvazioni", hint: "decisioni che aspettano te" },
  files: { title: "File", hint: "workspace, anteprime e upload" },
};

function initialTab(): Tab {
  const requested = new URLSearchParams(window.location.search).get("tab");
  if (
    requested === "workflows" ||
    requested === "runs" ||
    requested === "approvals" ||
    requested === "files"
  ) {
    return requested;
  }
  return "chat";
}

export default function App() {
  const [config, setConfig] = useState<ConnectionConfig>(() => loadConfig());
  const [tab, setTab] = useState<Tab>(initialTab);
  const [view, setView] = useState<View>(() => (loadConfig().token ? "tabs" : "settings"));
  const [runId, setRunId] = useState<number | null>(null);
  const [pending, setPending] = useState(0);

  const refreshPending = useCallback(async () => {
    if (!config.token && !config.baseUrl) return;
    try {
      const items = await api.interrupts(config);
      setPending(items.length);
    } catch {
      // Offline / misconfigured: keep the last known count.
    }
  }, [config]);

  usePolling(() => void refreshPending(), 15000, Boolean(config.baseUrl));

  useEffect(() => {
    if (view === "tabs" && tab === "approvals") void refreshPending();
  }, [view, tab, refreshPending]);

  // A notification tap asks the open app to jump to Approvals.
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent) => {
      if (event.data && event.data.type === "open-approvals") {
        setTab("approvals");
        setView("tabs");
      }
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, []);

  const handleSaveConfig = (next: ConnectionConfig) => {
    setConfig(next);
    saveConfig(next);
    if (next.token) setView("tabs");
  };

  const openRun = (id: number) => {
    setRunId(id);
    setView("run");
  };

  const header = view === "run" ? { title: "Esecuzione", hint: "stato e approvazioni" } : TAB_TITLES[tab];

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>{view === "settings" ? "Impostazioni" : header.title}</h1>
          <p className="hint">{view === "settings" ? "connessione al server" : header.hint}</p>
        </div>
        {view !== "settings" ? (
          <button
            className="btn small ghost"
            aria-label="Impostazioni"
            onClick={() => setView("settings")}
          >
            ⚙︎
          </button>
        ) : null}
      </header>

      {view === "settings" ? (
        <SettingsScreen config={config} onSave={handleSaveConfig} />
      ) : view === "run" && runId != null ? (
        <RunDetailScreen config={config} runId={runId} onBack={() => setView("tabs")} />
      ) : (
        <>
          {tab === "chat" ? (
            <ChatScreen config={config} onStarted={(id) => openRun(id)} />
          ) : null}
          {tab === "workflows" ? (
            <WorkflowsScreen config={config} onStarted={(id) => openRun(id)} />
          ) : null}
          {tab === "runs" ? <RunsScreen config={config} onOpen={openRun} /> : null}
          {tab === "files" ? <FilesScreen config={config} /> : null}
          {tab === "approvals" ? (
            <ApprovalsScreen
              config={config}
              onOpenRun={openRun}
              onPendingCount={(count) => setPending(count)}
            />
          ) : null}
        </>
      )}

      <nav className="bottom-nav" aria-label="Navigazione principale">
        {(["chat", "workflows", "runs", "approvals", "files"] as Tab[]).map((key) => (
          <button
            key={key}
            className={view === "tabs" && tab === key ? "active" : ""}
            onClick={() => {
              setTab(key);
              setView("tabs");
            }}
          >
            <span className="icon" aria-hidden="true">
              {key === "chat"
                ? "💬"
                : key === "workflows"
                  ? "🧩"
                  : key === "runs"
                    ? "📈"
                    : key === "approvals"
                      ? "✅"
                      : "📁"}
            </span>
            <span>
              {key === "chat"
                ? "Chat"
                : key === "workflows"
                  ? "Workflow"
                  : key === "runs"
                    ? "Run"
                    : key === "approvals"
                      ? "Approvazioni"
                      : "File"}
            </span>
            {key === "approvals" && pending > 0 ? (
              <span className="badge">{pending > 9 ? "9+" : pending}</span>
            ) : null}
          </button>
        ))}
      </nav>
    </div>
  );
}
