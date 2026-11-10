export type Language = "it" | "en";

export const translations = {
  it: {
    // Header & Nav
    assistantTitle: "Alfredo",
    assistantHint: "assistente libero",
    workflowsTitle: "Workflow",
    workflowsHint: "avvia un processo salvato",
    runsTitle: "Esecuzioni",
    runsHint: "storico e stato in tempo reale",
    approvalsTitle: "Approvazioni",
    approvalsHint: "decisioni che aspettano te",
    filesTitle: "File",
    filesHint: "workspace, anteprime e upload",
    settingsTitle: "Impostazioni",
    settingsHint: "connessione al server",
    runDetailTitle: "Esecuzione",
    runDetailHint: "stato e approvazioni",
    chatTab: "Chat",
    workflowsTab: "Workflow",
    runsTab: "Run",
    approvalsTab: "Approvazioni",
    filesTab: "File",
    close: "Chiudi",
    settings: "Impostazioni",

    // Common
    up: "Su",
    back: "Indietro",
    save: "Salva",
    saved: "Impostazioni salvate.",
    testConnection: "Test connessione",
    verifying: "Verifico…",
    refresh: "Aggiorna",
    loading: "Carico…",
    delete: "Elimina",
    download: "Scarica",
    status: "Stato",

    // Language selector
    languageTitle: "Lingua / Language",
    languageHint: "Seleziona la lingua dell'interfaccia.",

    // Settings
    serverTitle: "Server Alfredo",
    serverHint: "Indirizzo della Control API (es. http://192.168.1.20:8000 o dominio ngrok) e token di controllo.",
    controlToken: "Control token",
    systemGpuTitle: "Sistema e GPU",
    pushTitle: "Notifiche push",
    pushHint: "Ricevi notifiche sul telefono quando Alfredo richiede un'approvazione umana.",
    enablePush: "Abilita notifiche",
    disablePush: "Disabilita notifiche",
    sendTestPush: "Invia notifica di test",

    // Chat
    chatPlaceholder: "Scrivi un messaggio o chiedi un workflow…",
    send: "Invia",
    emptyChat: "Nessun messaggio ancora. Scrivi qualcosa per iniziare!",

    // Workflows
    startWorkflow: "Avvia",
    runningWorkflow: "In avvio…",
    noWorkflows: "Nessun workflow salvato disponibile.",

    // Approvals
    noApprovals: "Nessuna approvazione in attesa.",
    approve: "Approva",
    reject: "Rifiuta",
    inputPrompt: "Inserisci risposta…",

    // Files
    upload: "Carica",
    newFolder: "Nuova cartella",
    emptyFolder: "Questa cartella è vuota.",
  },
  en: {
    // Header & Nav
    assistantTitle: "Alfredo",
    assistantHint: "freeform assistant",
    workflowsTitle: "Workflows",
    workflowsHint: "run a saved process",
    runsTitle: "Runs",
    runsHint: "history & real-time status",
    approvalsTitle: "Approvals",
    approvalsHint: "decisions waiting for you",
    filesTitle: "Files",
    filesHint: "workspace, preview & upload",
    settingsTitle: "Settings",
    settingsHint: "server connection",
    runDetailTitle: "Run Detail",
    runDetailHint: "status & approvals",
    chatTab: "Chat",
    workflowsTab: "Workflows",
    runsTab: "Runs",
    approvalsTab: "Approvals",
    filesTab: "Files",
    close: "Close",
    settings: "Settings",

    // Common
    up: "Up",
    back: "Back",
    save: "Save",
    saved: "Settings saved.",
    testConnection: "Test connection",
    verifying: "Verifying…",
    refresh: "Refresh",
    loading: "Loading…",
    delete: "Delete",
    download: "Download",
    status: "Status",

    // Language selector
    languageTitle: "Language / Lingua",
    languageHint: "Choose interface language.",

    // Settings
    serverTitle: "Alfredo Server",
    serverHint: "Control API address (e.g. http://192.168.1.20:8000 or ngrok domain) and control token.",
    controlToken: "Control token",
    systemGpuTitle: "System & GPU",
    pushTitle: "Push Notifications",
    pushHint: "Receive alerts on your phone when Alfredo requires human approval.",
    enablePush: "Enable notifications",
    disablePush: "Disable notifications",
    sendTestPush: "Send test notification",

    // Chat
    chatPlaceholder: "Type a message or ask for a workflow…",
    send: "Send",
    emptyChat: "No messages yet. Say hello to get started!",

    // Workflows
    startWorkflow: "Run",
    runningWorkflow: "Starting…",
    noWorkflows: "No saved workflows available.",

    // Approvals
    noApprovals: "No pending approvals.",
    approve: "Approve",
    reject: "Reject",
    inputPrompt: "Type your answer…",

    // Files
    upload: "Upload",
    newFolder: "New folder",
    emptyFolder: "This folder is empty.",
  },
} as const;

export type TranslationKey = keyof typeof translations.it;

const LANG_KEY = "alfredo.lang.v1";

export function loadLanguage(): Language {
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved === "it" || saved === "en") return saved;
    if (typeof navigator !== "undefined" && navigator.language?.startsWith("en")) {
      return "en";
    }
  } catch {
    // fallback
  }
  return "it";
}

export function saveLanguage(lang: Language): void {
  try {
    localStorage.setItem(LANG_KEY, lang);
  } catch {
    // ignore
  }
}
