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
import {
  LanguageContext,
  loadLanguage,
  saveLanguage,
  translations,
  type Language,
  type TranslationKey,
} from "./i18n";

type View = "tabs" | "settings" | "run";

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
  const [masterModel, setMasterModel] = useState<string | null>(null);
  const [lang, setLang] = useState<Language>(() => loadLanguage());

  const handleLanguageChange = (nextLang: Language) => {
    setLang(nextLang);
    saveLanguage(nextLang);
  };

  const toggleLanguage = () => {
    handleLanguageChange(lang === "it" ? "en" : "it");
  };

  const t = (key: TranslationKey): string => translations[lang][key] || "";

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

  const tabTitles: Record<Tab, { title: string; hint: string }> = {
    chat: { title: t("assistantTitle"), hint: t("assistantHint") },
    workflows: { title: t("workflowsTitle"), hint: t("workflowsHint") },
    runs: { title: t("runsTitle"), hint: t("runsHint") },
    approvals: { title: t("approvalsTitle"), hint: t("approvalsHint") },
    files: { title: t("filesTitle"), hint: t("filesHint") },
  };

  const currentHeader =
    view === "run"
      ? { title: t("runDetailTitle"), hint: t("runDetailHint") }
      : tabTitles[tab];

  const currentTitle = view === "settings" ? t("settingsTitle") : currentHeader.title;
  const currentHint = view === "settings" ? t("settingsHint") : currentHeader.hint;

  return (
    <LanguageContext.Provider value={{ lang, setLang: handleLanguageChange, toggleLanguage, t }}>
      <div className="app">
        <header className="app-header">
          <div className="header-title-container">
            <div className="header-title-row">
              <h1>{currentTitle}</h1>
              <img
                src="/app/alfredo_robot.png"
                alt="Alfredo"
                className="header-robot-icon"
                title="Alfredo Assistant"
              />
            </div>
            <div className="header-hint-row">
              <p className="hint">{currentHint}</p>
              {view === "tabs" && tab === "chat" && masterModel ? (
                <span className="header-model-tag" title={`Master AI: ${masterModel}`}>
                  <span className="header-model-dot" aria-hidden="true" />
                  {masterModel}
                </span>
              ) : null}
            </div>
          </div>

          <div className="header-actions">
            <button
              className="btn small ghost lang-toggle-btn"
              onClick={toggleLanguage}
              title={lang === "it" ? "Switch to English" : "Passa all'italiano"}
              aria-label="Cambia lingua"
            >
              {lang === "it" ? "🇮🇹 IT" : "🇬🇧 EN"}
            </button>
            {view !== "settings" ? (
              <button
                className="btn small ghost"
                aria-label={t("settings")}
                onClick={() => setView("settings")}
                title={t("settings")}
              >
                ⚙︎
              </button>
            ) : (
              <button
                className="btn small ghost"
                aria-label={t("close")}
                onClick={() => setView("tabs")}
                title={t("close")}
              >
                ✕
              </button>
            )}
          </div>
        </header>

        {view === "settings" ? (
          <SettingsScreen
            config={config}
            onSave={handleSaveConfig}
            lang={lang}
            onLanguageChange={handleLanguageChange}
          />
        ) : view === "run" && runId != null ? (
          <RunDetailScreen
            config={config}
            runId={runId}
            onBack={() => setView("tabs")}
            lang={lang}
          />
        ) : (
          <>
            {tab === "chat" ? (
              <ChatScreen
                config={config}
                onStarted={(id) => openRun(id)}
                onMasterModel={setMasterModel}
              />
            ) : null}
            {tab === "workflows" ? (
              <WorkflowsScreen config={config} onStarted={(id) => openRun(id)} />
            ) : null}
            {tab === "runs" ? <RunsScreen config={config} onOpen={openRun} /> : null}
            {tab === "files" ? <FilesScreen config={config} lang={lang} /> : null}
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
                  ? t("chatTab")
                  : key === "workflows"
                    ? t("workflowsTab")
                    : key === "runs"
                      ? t("runsTab")
                      : key === "approvals"
                        ? t("approvalsTab")
                        : t("filesTab")}
              </span>
              {key === "approvals" && pending > 0 ? (
                <span className="badge">{pending > 9 ? "9+" : pending}</span>
              ) : null}
            </button>
          ))}
        </nav>
      </div>
    </LanguageContext.Provider>
  );
}
