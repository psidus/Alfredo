import { createContext, useContext } from "react";

export type Language = "it" | "en";

export const translations = {
  it: {
    // Header & Navigation
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

    // Common actions & status
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
    cancel: "Annulla",
    confirm: "Conferma",
    justNow: "adesso",
    minAgo: "min fa",
    hoursAgo: "h fa",

    // Status chips
    statusNeedsYou: "attende te",
    statusCompleted: "completato",
    statusError: "errore",
    statusRunning: "in corso",
    statusPaused: "in pausa",
    statusCancelled: "annullato",

    // Language
    languageTitle: "Lingua dell'applicazione",
    languageHint: "Scegli la lingua preferita per i testi dell'applicazione.",

    // Settings
    serverTitle: "Server Alfredo",
    serverHint: "Indirizzo della Control API (es. http://192.168.1.20:8000 o dominio ngrok) e token di controllo.",
    controlToken: "Control token",
    systemGpuTitle: "Sistema e GPU",
    pushTitle: "Notifiche push",
    pushHint: "Ricevi notifiche sul telefono quando Alfredo richiede un'approvazione umana.",
    enablePush: "Attiva notifiche",
    disablePush: "Disattiva",
    sendTestPush: "Prova notifica",
    installNotesTitle: "Installazione & Note",
    installNotesHint: "Usa 'Installa applicazione' (Chrome su Android) o 'Aggiungi a schermata Home' (Safari su iOS) per usare Alfredo a schermo intero come una vera app nativa.",
    gpuQueue: "Coda GPU",
    gpuIdle: "libera",
    callsServed: "Chiamate servite",
    timeouts: "attese scadute",
    avgWait: "attesa media",

    // Chat
    chatHeaderHint: "Chat libera: l'assistente può proporre un workflow da avviare.",
    newChat: "Nuova chat",
    chatEmptyPrompt: "Chiedi qualcosa ad Alfredo.",
    chatEmptyExample: "Es. “Esegui il report termico sui dati di ieri”.",
    chatInputPlaceholder: "Scrivi un messaggio…",
    send: "Invia",
    gpuBusy: "GPU occupata",
    inQueue: "in coda",
    behindRun: "dietro run",
    proposed: "proposto",
    requiredValue: "valore richiesto",
    startWorkflow: "Avvia workflow",
    starting: "Avvio…",
    missingFields: "Mancano",
    noTextReply: "(nessuna risposta testuale)",

    // Workflows
    savedWorkflowsCount: "workflow salvati",
    noWorkflowsYet: "Nessun workflow ancora",
    loadingWorkflows: "Carico i workflow…",
    createWorkflowHint: "Crea un workflow dalla dashboard per vederlo qui.",
    inputLabel: "input",
    inputsLabel: "input",
    expectedOutputs: "Output attesi",
    runButton: "Avvia",

    // Runs
    autoRefreshEvery5s: "Aggiornamento automatico ogni 5 s",
    loadingRuns: "Carico le esecuzioni…",
    noRunsYet: "Nessuna esecuzione. Avvia un workflow dalla chat o dalla lista.",

    // Approvals
    pendingApprovalsCount: "approvazioni in attesa",
    noApprovalsYet: "Nessuna approvazione",
    approvalsEmptyHint: "Quando un workflow si ferma per una conferma, la trovi qui.",
    answerOrFeedback: "Risposta / feedback",
    answerPlaceholder: "Scegli un'opzione o scrivi un feedback…",
    sendAndResume: "Invia e riprendi",
    sending: "Invio…",
    openRun: "Apri run",
    confirmStep: "Confermi il passaggio?",

    // RunDetail
    pausedNeedsApproval: "In pausa: richiesta approvazione",
    answerToContinue: "Rispondi per continuare",
    sendAnswer: "Invia risposta",
    manualPause: "Pausa manuale",
    pauseExecution: "Metti in pausa",
    resumeExecution: "Riprendi",
    cancelExecution: "Annulla esecuzione",
    eventsTitle: "Eventi",
    filesExportTitle: "File ed export",
    noFilesGenerated: "Nessun file generato finora.",
    partialPreview: "Anteprima parziale.",
    sheetLabel: "Foglio",

    // Run Events
    evStarted: "Avviato",
    evPaused: "In pausa",
    evFinished: "Completato",
    evFailed: "Fallito",
    evCancelled: "Annullato",
    evNodeStarted: "Nodo avviato",
    evNodeCompleted: "Nodo completato",
    evNodeFailed: "Nodo fallito",
    evInterruptOpened: "Approvazione richiesta",
    evInterruptAnswered: "Approvazione ricevuta",
    evAutomation: "Automazione",

    // Files
    loadingFiles: "Carico i file…",
    emptyFolder: "Questa cartella è vuota.",
    newFolder: "Nuova cartella",
    folderNamePlaceholder: "Nome cartella…",
    create: "Crea",
    uploadFile: "Carica file",
    uploading: "Caricamento in corso…",
    shareLink: "Condividi",
    linkCopied: "Link copiato negli appunti!",
    copyLink: "Copia link",
    preview: "Anteprima",
  },
  en: {
    // Header & Navigation
    assistantTitle: "Alfredo",
    assistantHint: "freeform assistant",
    workflowsTitle: "Workflows",
    workflowsHint: "launch a saved process",
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

    // Common actions & status
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
    cancel: "Cancel",
    confirm: "Confirm",
    justNow: "just now",
    minAgo: "m ago",
    hoursAgo: "h ago",

    // Status chips
    statusNeedsYou: "needs you",
    statusCompleted: "completed",
    statusError: "error",
    statusRunning: "running",
    statusPaused: "paused",
    statusCancelled: "cancelled",

    // Language
    languageTitle: "Application Language",
    languageHint: "Choose your preferred language for the application interface.",

    // Settings
    serverTitle: "Alfredo Server",
    serverHint: "Control API address (e.g. http://192.168.1.20:8000 or ngrok domain) and control token.",
    controlToken: "Control token",
    systemGpuTitle: "System & GPU",
    pushTitle: "Push Notifications",
    pushHint: "Receive alerts on your phone when Alfredo requires human approval.",
    enablePush: "Enable notifications",
    disablePush: "Disable",
    sendTestPush: "Send test notification",
    installNotesTitle: "Installation & Notes",
    installNotesHint: "Use 'Install app' (Chrome on Android) or 'Add to Home Screen' (Safari on iOS) to use Alfredo full-screen like a native app.",
    gpuQueue: "GPU Queue",
    gpuIdle: "idle",
    callsServed: "Calls served",
    timeouts: "timeouts",
    avgWait: "avg wait",

    // Chat
    chatHeaderHint: "Freeform chat: the assistant can suggest workflows to run.",
    newChat: "New chat",
    chatEmptyPrompt: "Ask Alfredo anything.",
    chatEmptyExample: "E.g. “Run the thermal report on yesterday's data”.",
    chatInputPlaceholder: "Type a message…",
    send: "Send",
    gpuBusy: "GPU busy",
    inQueue: "in queue",
    behindRun: "behind run",
    proposed: "suggested",
    requiredValue: "required value",
    startWorkflow: "Start workflow",
    starting: "Starting…",
    missingFields: "Missing",
    noTextReply: "(no text reply)",

    // Workflows
    savedWorkflowsCount: "saved workflows",
    noWorkflowsYet: "No workflows yet",
    loadingWorkflows: "Loading workflows…",
    createWorkflowHint: "Create a workflow from the desktop dashboard to see it here.",
    inputLabel: "input",
    inputsLabel: "inputs",
    expectedOutputs: "Expected outputs",
    runButton: "Run",

    // Runs
    autoRefreshEvery5s: "Auto-refreshing every 5 s",
    loadingRuns: "Loading runs…",
    noRunsYet: "No runs yet. Start a workflow from chat or from the list.",

    // Approvals
    pendingApprovalsCount: "pending approvals",
    noApprovalsYet: "No approvals",
    approvalsEmptyHint: "When a workflow pauses for human confirmation, you will find it here.",
    answerOrFeedback: "Answer / feedback",
    answerPlaceholder: "Select an option or type feedback…",
    sendAndResume: "Send and resume",
    sending: "Sending…",
    openRun: "Open run",
    confirmStep: "Confirm this step?",

    // RunDetail
    pausedNeedsApproval: "Paused: approval required",
    answerToContinue: "Answer to continue",
    sendAnswer: "Submit answer",
    manualPause: "Manual pause",
    pauseExecution: "Pause",
    resumeExecution: "Resume",
    cancelExecution: "Cancel run",
    eventsTitle: "Events",
    filesExportTitle: "Files & exports",
    noFilesGenerated: "No files generated yet.",
    partialPreview: "Partial preview.",
    sheetLabel: "Sheet",

    // Run Events
    evStarted: "Started",
    evPaused: "Paused",
    evFinished: "Completed",
    evFailed: "Failed",
    evCancelled: "Cancelled",
    evNodeStarted: "Node started",
    evNodeCompleted: "Node completed",
    evNodeFailed: "Node failed",
    evInterruptOpened: "Approval requested",
    evInterruptAnswered: "Approval received",
    evAutomation: "Automation",

    // Files
    loadingFiles: "Loading files…",
    emptyFolder: "This folder is empty.",
    newFolder: "New folder",
    folderNamePlaceholder: "Folder name…",
    create: "Create",
    uploadFile: "Upload file",
    uploading: "Uploading…",
    shareLink: "Share",
    linkCopied: "Link copied to clipboard!",
    copyLink: "Copy link",
    preview: "Preview",
  },
} as const;

export type TranslationKey = keyof typeof translations.it;

export interface LanguageContextValue {
  lang: Language;
  setLang: (lang: Language) => void;
  toggleLanguage: () => void;
  t: (key: TranslationKey) => string;
}

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

export const LanguageContext = createContext<LanguageContextValue>({
  lang: "it",
  setLang: () => {},
  toggleLanguage: () => {},
  t: (key: TranslationKey) => translations.it[key] || "",
});

export function useLanguage() {
  return useContext(LanguageContext);
}
