import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { disablePush, enablePush, pushStatus, sendTestPush } from "../push";
import type { PushStatus } from "../push";
import type { ConnectionConfig, SystemStatus } from "../types";
import { ErrorBanner, usePolling } from "../components/ui";
import { translations, type Language } from "../i18n";

interface Props {
  config: ConnectionConfig;
  onSave: (config: ConnectionConfig) => void;
  lang?: Language;
  onLanguageChange?: (lang: Language) => void;
}

export function SettingsScreen({ config, onSave, lang = "it", onLanguageChange }: Props) {
  const [baseUrl, setBaseUrl] = useState(config.baseUrl);
  const [token, setToken] = useState(config.token);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [push, setPush] = useState<PushStatus | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  const [pushMessage, setPushMessage] = useState<string | null>(null);
  const [pushError, setPushError] = useState<string | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);

  const t = translations[lang];

  useEffect(() => {
    void pushStatus().then(setPush);
  }, []);

  const loadSystem = async () => {
    if (!config.baseUrl) return;
    try {
      setSystem(await api.systemStatus(config));
    } catch {
      // The card is informational: keep the last snapshot when offline.
    }
  };

  usePolling(() => void loadSystem(), 15000, Boolean(config.baseUrl));

  const liveConfig: ConnectionConfig = {
    baseUrl: baseUrl.trim(),
    token: token.trim(),
  };

  const save = () => {
    onSave(liveConfig);
    setStatus(t.saved);
    setError(null);
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const health = await api.health(liveConfig);
      setStatus(
        lang === "it"
          ? `Connessione OK · versione ${health.version ?? "?"}`
          : `Connection OK · version ${health.version ?? "?"}`
      );
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const activatePush = async () => {
    setPushBusy(true);
    setPushError(null);
    setPushMessage(null);
    onSave(liveConfig);
    try {
      await enablePush(liveConfig);
      setPush(await pushStatus());
      setPushMessage(
        lang === "it"
          ? "Notifiche attive su questo dispositivo."
          : "Notifications enabled on this device."
      );
    } catch (exc) {
      setPushError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setPushBusy(false);
    }
  };

  const deactivatePush = async () => {
    setPushBusy(true);
    setPushError(null);
    setPushMessage(null);
    try {
      await disablePush(liveConfig);
      setPush(await pushStatus());
      setPushMessage(
        lang === "it"
          ? "Notifiche disattivate su questo dispositivo."
          : "Notifications disabled on this device."
      );
    } catch (exc) {
      setPushError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setPushBusy(false);
    }
  };

  const testPush = async () => {
    setPushBusy(true);
    setPushError(null);
    setPushMessage(null);
    try {
      const delivered = await sendTestPush(liveConfig);
      setPushMessage(
        lang === "it"
          ? `Notifica inviata a ${delivered} dispositivo/i.`
          : `Notification sent to ${delivered} device(s).`
      );
    } catch (exc) {
      setPushError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setPushBusy(false);
    }
  };

  return (
    <div className="screen settings-screen">
      <div className="scroll-area settings-scroll">
        {/* Card selezione lingua */}
        <div className="card">
          <h2>{t.languageTitle}</h2>
          <p className="muted">{t.languageHint}</p>
          <div className="actions" style={{ marginTop: "10px" }}>
            <button
              className={`btn ${lang === "it" ? "primary" : "ghost"}`}
              onClick={() => onLanguageChange?.("it")}
            >
              🇮🇹 Italiano
            </button>
            <button
              className={`btn ${lang === "en" ? "primary" : "ghost"}`}
              onClick={() => onLanguageChange?.("en")}
            >
              🇬🇧 English
            </button>
          </div>
        </div>

        {/* Card Server */}
        <div className="card">
          <h2>{t.serverTitle}</h2>
          <p className="muted">{t.serverHint}</p>

          <div className="field">
            <label htmlFor="base-url">Base URL</label>
            <input
              id="base-url"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://inexpugnable-marvis-nonvalid.ngrok-free.app"
            />
          </div>

          <div className="field">
            <label htmlFor="control-token">{t.controlToken}</label>
            <input
              id="control-token"
              type="password"
              autoCapitalize="off"
              autoCorrect="off"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="ALFREDO_CONTROL_TOKEN"
            />
          </div>

          {status ? <div className="banner">{status}</div> : null}
          <ErrorBanner message={error} />

          <div className="actions">
            <button className="btn primary" onClick={save}>
              {t.save}
            </button>
            <button className="btn" onClick={() => void test()} disabled={busy}>
              {busy ? t.verifying : t.testConnection}
            </button>
          </div>
        </div>

        {/* Card Sistema e GPU */}
        <div className="card">
          <h3>{t.systemGpuTitle}</h3>
          {!system ? (
            <p className="muted">
              {lang === "it"
                ? "Dati non disponibili: server non raggiungibile."
                : "Data unavailable: server unreachable."}
            </p>
          ) : (
            <>
              <p className="muted">
                {system.gpu.available
                  ? `${system.gpu.name || "GPU"} · ${((system.gpu.free_mb ?? 0) / 1024).toFixed(1)} GB ${lang === "it" ? "liberi" : "free"}`
                  : system.gpu.error || (lang === "it" ? "GPU non rilevata" : "GPU not detected")}
              </p>
              <div className="file-row">
                <div className="file-meta">
                  <strong>{lang === "it" ? "Coda GPU" : "GPU Queue"}</strong>
                  <span className="muted">
                    {system.queue?.current
                      ? `${system.queue.current.profile}${
                          system.queue.current.run_id
                            ? ` · run #${system.queue.current.run_id}`
                            : ""
                        }`
                      : lang === "it" ? "libera" : "idle"}
                    {system.queue?.waiting?.length
                      ? ` · ${system.queue.waiting.length} ${lang === "it" ? "in attesa" : "waiting"}${
                          system.queue.waiting[0].run_id
                            ? ` behind #${system.queue.waiting[0].run_id}`
                            : ""
                        }${
                          system.queue.waiting[0].eta_s != null
                            ? ` · ~${Math.round(system.queue.waiting[0].eta_s)} s`
                            : ""
                        }`
                      : ""}
                  </span>
                </div>
              </div>
              {Object.values(system.profiles ?? {}).map((profile) => (
                <div className="file-row" key={profile.name}>
                  <div className="file-meta">
                    <strong>
                      {profile.name} · {profile.model}
                    </strong>
                    <span className="muted">
                      {profile.fits === false
                        ? (lang === "it" ? "non entra in VRAM" : "exceeds VRAM")
                        : profile.fits
                          ? (lang === "it" ? "entra in VRAM" : "fits VRAM")
                          : (lang === "it" ? "VRAM non nota" : "unknown VRAM")}
                      {profile.estimated_vram_mb ? ` · ~${profile.estimated_vram_mb} MB` : ""}
                      {profile.headroom_mb != null ? ` · headroom ${profile.headroom_mb} MB` : ""}
                      {profile.avg_call_s != null ? ` · ~${profile.avg_call_s} s/call` : ""}
                    </span>
                  </div>
                </div>
              ))}
              <p className="muted">
                {lang === "it" ? "Chiamate servite: " : "Calls served: "}
                {system.metrics?.served ?? 0} ·{" "}
                {lang === "it" ? "attese scadute: " : "timeouts: "}
                {system.metrics?.timeouts ?? 0}
                {system.metrics?.avg_wait_s != null
                  ? ` · ${lang === "it" ? "attesa media" : "avg wait"} ${system.metrics.avg_wait_s.toFixed(1)} s`
                  : ""}
              </p>
            </>
          )}
        </div>

        {/* Card Notifiche push */}
        <div className="card">
          <h3>{t.pushTitle}</h3>
          <p className="muted">{t.pushHint}</p>

          {push === null ? (
            <p className="muted">{lang === "it" ? "Verifico il supporto…" : "Checking support…"}</p>
          ) : !push.supported ? (
            <p className="muted">
              {lang === "it"
                ? "Questo browser non supporta le notifiche push. Su iOS serve iOS 16.4+ con l'app aggiunta alla schermata Home."
                : "This browser does not support push notifications. On iOS, iOS 16.4+ is required with app added to Home Screen."}
            </p>
          ) : push.permission === "denied" ? (
            <p className="banner">
              {lang === "it"
                ? "Notifiche bloccate dal browser: riabilita i permessi per questo sito dalle impostazioni del telefono."
                : "Notifications blocked by browser: enable site permissions in phone settings."}
            </p>
          ) : (
            <p className="muted">
              {lang === "it" ? "Stato: " : "Status: "}
              <strong>
                {push.subscribed
                  ? (lang === "it" ? "attive" : "active")
                  : (lang === "it" ? "non attive" : "disabled")}
              </strong>
              {push.endpoint ? (lang === "it" ? " · dispositivo registrato" : " · device registered") : ""}
            </p>
          )}

          {pushMessage ? <div className="banner">{pushMessage}</div> : null}
          <ErrorBanner message={pushError} onDismiss={() => setPushError(null)} />

          {push?.supported && push.permission !== "denied" ? (
            <div className="actions">
              {!push.subscribed ? (
                <button className="btn primary" onClick={() => void activatePush()} disabled={pushBusy}>
                  {pushBusy ? (lang === "it" ? "Attivo…" : "Enabling…") : t.enablePush}
                </button>
              ) : (
                <>
                  <button className="btn" onClick={() => void testPush()} disabled={pushBusy}>
                    {pushBusy ? (lang === "it" ? "Invio…" : "Sending…") : t.sendTestPush}
                  </button>
                  <button className="btn ghost" onClick={() => void deactivatePush()} disabled={pushBusy}>
                    {t.disablePush}
                  </button>
                </>
              )}
            </div>
          ) : null}
        </div>

        {/* Card Informazioni */}
        <div className="card">
          <h3>{lang === "it" ? "Installazione & Note" : "Installation & Notes"}</h3>
          <p className="muted">
            {lang === "it"
              ? "Usa 'Installa applicazione' (Chrome su Android) o 'Aggiungi a schermata Home' (Safari su iOS) per usare Alfredo a schermo intero come una vera app nativa."
              : "Use 'Install app' (Chrome on Android) or 'Add to Home Screen' (Safari on iOS) to use Alfredo full-screen like a native app."}
          </p>
        </div>
      </div>
    </div>
  );
}
