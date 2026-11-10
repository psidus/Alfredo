import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { disablePush, enablePush, pushStatus, sendTestPush } from "../push";
import type { PushStatus } from "../push";
import type { ConnectionConfig, SystemStatus } from "../types";
import { ErrorBanner, usePolling } from "../components/ui";

interface Props {
  config: ConnectionConfig;
  onSave: (config: ConnectionConfig) => void;
}

export function SettingsScreen({ config, onSave }: Props) {
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
    setStatus("Impostazioni salvate.");
    setError(null);
  };

  const test = async () => {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const health = await api.health(liveConfig);
      setStatus(`Connessione OK · versione ${health.version ?? "?"}`);
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
      setPushMessage("Notifiche attive su questo dispositivo.");
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
      setPushMessage("Notifiche disattivate su questo dispositivo.");
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
      setPushMessage(`Notifica inviata a ${delivered} dispositivo/i.`);
    } catch (exc) {
      setPushError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setPushBusy(false);
    }
  };

  return (
    <div className="screen">
      <div className="scroll-area">
        <div className="card">
          <h2>Server Alfredo</h2>
          <p className="muted">
            Indirizzo della Control API (es. <code>http://192.168.1.20:8000</code>) e token di
            controllo.
          </p>

          <div className="field">
            <label htmlFor="base-url">Base URL</label>
            <input
              id="base-url"
              inputMode="url"
              autoCapitalize="off"
              autoCorrect="off"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="http://192.168.1.20:8000"
            />
          </div>

          <div className="field">
            <label htmlFor="control-token">Control token</label>
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
              Salva
            </button>
            <button className="btn" onClick={() => void test()} disabled={busy}>
              {busy ? "Verifico…" : "Test connessione"}
            </button>
          </div>
        </div>

        <div className="card">
          <h3>Sistema e GPU</h3>
          {!system ? (
            <p className="muted">Dati non disponibili: server non raggiungibile.</p>
          ) : (
            <>
              <p className="muted">
                {system.gpu.available
                  ? `${system.gpu.name || "GPU"} · ${((system.gpu.free_mb ?? 0) / 1024).toFixed(1)} GB liberi`
                  : system.gpu.error || "GPU non rilevata (nvidia-smi assente)"}
              </p>
              <div className="file-row">
                <div className="file-meta">
                  <strong>Coda GPU</strong>
                  <span className="muted">
                    {system.queue?.current
                      ? `${system.queue.current.profile}${
                          system.queue.current.run_id
                            ? ` · run #${system.queue.current.run_id}`
                            : ""
                        }`
                      : "libera"}
                    {system.queue?.waiting?.length
                      ? ` · ${system.queue.waiting.length} in attesa${
                          system.queue.waiting[0].run_id
                            ? ` dietro run #${system.queue.waiting[0].run_id}`
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
                        ? "non entra in VRAM"
                        : profile.fits
                          ? "entra in VRAM"
                          : "VRAM non nota"}
                      {profile.estimated_vram_mb ? ` · ~${profile.estimated_vram_mb} MB` : ""}
                      {profile.headroom_mb != null ? ` · margine ${profile.headroom_mb} MB` : ""}
                      {profile.avg_call_s != null ? ` · ~${profile.avg_call_s} s/chiamata` : ""}
                    </span>
                  </div>
                </div>
              ))}
              <p className="muted">
                Chiamate servite: {system.metrics?.served ?? 0} · attese scadute:{" "}
                {system.metrics?.timeouts ?? 0}
                {system.metrics?.avg_wait_s != null
                  ? ` · attesa media ${system.metrics.avg_wait_s.toFixed(1)} s`
                  : ""}
                {system.metrics?.evictions
                  ? ` · cambi modello: ${system.metrics.evictions}`
                  : ""}
              </p>
              {Object.entries(system.metrics?.first_token ?? {}).map(([name, value]) => (
                <p className="muted" key={name}>
                  Primo token {name}:{" "}
                  {value.avg_s != null ? `${value.avg_s.toFixed(2)} s medi` : "n/d"} ·{" "}
                  {value.count} campioni
                </p>
              ))}
            </>
          )}
        </div>

        <div className="card">
          <h3>Notifiche push</h3>
          <p className="muted">
            Ricevi sul telefono le richieste di approvazione anche con l'app chiusa. Richiede la
            versione servita dal server (<code>/app/</code>) e <code>pywebpush</code> installato.
          </p>

          {push === null ? (
            <p className="muted">Verifico il supporto…</p>
          ) : !push.supported ? (
            <p className="muted">
              Questo browser non supporta le notifiche push. Su iOS serve iOS 16.4+ con l'app
              aggiunta alla schermata Home.
            </p>
          ) : push.permission === "denied" ? (
            <p className="banner">
              Notifiche bloccate dal browser: riabilita i permessi per questo sito dalle
              impostazioni del telefono.
            </p>
          ) : (
            <p className="muted">
              Stato: <strong>{push.subscribed ? "attive" : "non attive"}</strong>
              {push.endpoint ? " · dispositivo registrato" : ""}
            </p>
          )}

          {pushMessage ? <div className="banner">{pushMessage}</div> : null}
          <ErrorBanner message={pushError} onDismiss={() => setPushError(null)} />

          {push?.supported && push.permission !== "denied" ? (
            <div className="actions">
              {!push.subscribed ? (
                <button className="btn primary" onClick={() => void activatePush()} disabled={pushBusy}>
                  {pushBusy ? "Attivo…" : "Attiva notifiche"}
                </button>
              ) : (
                <>
                  <button className="btn" onClick={() => void testPush()} disabled={pushBusy}>
                    {pushBusy ? "Invio…" : "Prova notifica"}
                  </button>
                  <button className="btn ghost" onClick={() => void deactivatePush()} disabled={pushBusy}>
                    Disattiva
                  </button>
                </>
              )}
            </div>
          ) : null}
        </div>

        <div className="card">
          <h3>Installazione come app</h3>
          <p className="muted">
            Dopo aver aperto questa pagina nel browser del telefono, usa “Aggiungi alla schermata
            Home” (iOS Safari) o “Installa app” (Chrome Android). L'interfaccia resta disponibile
            offline; le chiamate al server richiedono la rete.
          </p>
        </div>

        <div className="card">
          <h3>Note</h3>
          <p className="muted">
            Il token viene salvato solo su questo dispositivo. La Control API deve essere
            raggiungibile sulla rete locale e <code>ALFREDO_CONTROL_TOKEN</code> deve coincidere con
            quello impostato sul server.
          </p>
        </div>
      </div>
    </div>
  );
}
