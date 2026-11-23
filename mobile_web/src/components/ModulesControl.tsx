import { useEffect, useState } from "react";
import { api } from "../api";
import { useLanguage } from "../i18n";
import {
  DEFAULT_ENABLED_MODULES,
  loadChatSettings,
  saveChatSettings,
  type ChatSettings,
} from "../storage";
import type { ConnectionConfig, ContextMode, ModuleInfo } from "../types";

const FALLBACK_MODULES: ModuleInfo[] = [
  {
    id: "workflows",
    label: "Workflows & Runs",
    description: "List/start saved workflows, check/cancel runs, and answer approvals.",
    default_enabled: true,
    tools: [],
  },
  {
    id: "files_code",
    label: "Files & Codebase",
    description: "Inspect workspace files and codebases via Graphify or Repomix.",
    default_enabled: true,
    tools: [],
  },
  {
    id: "memory_kb",
    label: "Memory & Knowledge Base",
    description: "Save/recall user facts and search local vector knowledge bases.",
    default_enabled: true,
    tools: [],
  },
  {
    id: "assets_skills",
    label: "Assets, Skills & Architect",
    description: "Browse/load SKILL.md playbooks and draft workflow blueprints.",
    default_enabled: false,
    tools: [],
  },
  {
    id: "mcp_apps",
    label: "MCP, Apps & GPU",
    description: "Inspect connected apps, WireGuard GPU VRAM, and MCP server tools.",
    default_enabled: false,
    tools: [],
  },
];

export function ModulesPopoverControl({ config }: { config: ConnectionConfig }) {
  const { t } = useLanguage();
  const [open, setOpen] = useState(false);
  const [modules, setModules] = useState<ModuleInfo[]>(FALLBACK_MODULES);
  const [settings, setSettings] = useState<ChatSettings>(() => loadChatSettings());

  useEffect(() => {
    let active = true;
    api
      .modules(config)
      .then((list) => {
        if (active && Array.isArray(list) && list.length > 0) {
          setModules(list);
        }
      })
      .catch(() => {
        // Keep curated fallback list if offline / unconfigured
      });
    return () => {
      active = false;
    };
  }, [config.baseUrl, config.token]);

  const updateSettings = (next: ChatSettings) => {
    setSettings(next);
    saveChatSettings(next);
  };

  const toggleModule = (moduleId: string) => {
    const exists = settings.enabledModules.includes(moduleId);
    const enabledModules = exists
      ? settings.enabledModules.filter((id) => id !== moduleId)
      : [...settings.enabledModules, moduleId];
    updateSettings({ ...settings, enabledModules });
  };

  const setContextMode = (contextMode: ContextMode) => {
    updateSettings({ ...settings, contextMode });
  };

  const contextOptions: Array<{ id: ContextMode; label: string }> = [
    { id: "auto", label: t("contextAuto") },
    { id: "graphify", label: t("contextGraphify") },
    { id: "repomix", label: t("contextRepomix") },
  ];

  const activeCount = settings.enabledModules.length;

  return (
    <>
      <button
        type="button"
        className={`btn small ${open ? "primary" : "ghost"}`}
        onClick={() => setOpen((prev) => !prev)}
        title={t("modulesToggle")}
      >
        {t("modulesToggle")} ({activeCount})
      </button>

      {open ? (
        <div
          onClick={() => setOpen(false)}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 60,
            background: "rgba(4, 9, 20, 0.6)",
            backdropFilter: "blur(3px)",
            display: "flex",
            alignItems: "flex-start",
            justifyContent: "center",
            padding: "70px 16px 16px",
          }}
        >
          <div
            className="card"
            onClick={(event) => event.stopPropagation()}
            style={{
              width: "100%",
              maxWidth: "420px",
              maxHeight: "calc(100dvh - 96px)",
              overflowY: "auto",
              display: "flex",
              flexDirection: "column",
              gap: "12px",
              boxShadow: "0 20px 50px rgba(0, 0, 0, 0.55)",
            }}
          >
            <div className="row between">
              <strong>{t("modulesToggle")}</strong>
              <div className="row" style={{ gap: "6px" }}>
                <button
                  type="button"
                  className="btn small ghost"
                  onClick={() =>
                    updateSettings({
                      enabledModules: [...DEFAULT_ENABLED_MODULES],
                      contextMode: "auto",
                    })
                  }
                >
                  Reset
                </button>
                <button
                  type="button"
                  className="btn small ghost"
                  onClick={() => setOpen(false)}
                  aria-label="Close"
                >
                  ✕
                </button>
              </div>
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
              {modules.map((mod) => {
                const enabled = settings.enabledModules.includes(mod.id);
                return (
                  <button
                    key={mod.id}
                    type="button"
                    onClick={() => toggleModule(mod.id)}
                    style={{
                      textAlign: "left",
                      padding: "10px 12px",
                      borderRadius: "12px",
                      border: enabled
                        ? "1px solid rgba(56, 189, 248, 0.55)"
                        : "1px solid var(--border)",
                      background: enabled
                        ? "rgba(56, 189, 248, 0.1)"
                        : "var(--surface-2)",
                      display: "flex",
                      flexDirection: "column",
                      gap: "3px",
                    }}
                  >
                    <div className="row between" style={{ width: "100%" }}>
                      <span style={{ fontWeight: 600, fontSize: "0.88rem" }}>
                        {mod.label}
                      </span>
                      <span
                        className={`chip ${enabled ? "completed" : ""}`}
                        style={{ fontSize: "0.7rem" }}
                      >
                        {enabled ? "ON" : "OFF"}
                      </span>
                    </div>
                    <span className="muted" style={{ fontSize: "0.76rem" }}>
                      {mod.description}
                    </span>
                  </button>
                );
              })}
            </div>

            <div
              style={{
                borderTop: "1px solid var(--border)",
                paddingTop: "10px",
                display: "flex",
                flexDirection: "column",
                gap: "8px",
              }}
            >
              <span className="muted" style={{ fontSize: "0.8rem", fontWeight: 600 }}>
                {t("codeContextMode")}
              </span>
              <div className="row wrap" style={{ gap: "6px" }}>
                {contextOptions.map((opt) => {
                  const selected = settings.contextMode === opt.id;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      className={`btn small ${selected ? "primary" : "ghost"}`}
                      onClick={() => setContextMode(opt.id)}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
