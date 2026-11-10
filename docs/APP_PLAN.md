# Piano di implementazione — App di controllo remoto per Alfredo

> Documento di pianificazione. Nessuna decisione qui dentro richiede modifiche al codice esistente:
> ogni fase indica quali file nuovi creare, quali file esistenti toccare e come verificare il risultato.
>
> Stato: bozza 2 — 5 novembre 2026 (revisione: chat ownership, uso personale, lingua inglese)
> Obiettivo: un client (app/PWA) che faccia da telecomando per un'istanza Alfredo in esecuzione
> su un PC dell'utente. Telegram resta il client di avvio; **una volta che l'app è pronta, la chat
> passa di proprietà all'app** e Telegram scende a fallback.

### Decisioni di questa revisione

| # | Decisione | Effetto sul piano |
|---|---|---|
| R1 | La chat si sposta da Telegram all'app, ma **solo quando l'app la supporta** | La Fase 2 si riduce al minimo indispensabile (2A); la parità completa e il passaggio di consegne diventano la **Fase 7**, in coda |
| R2 | Pairing/identità multi-device **rimandati** | Fase 3 sostituita da un accesso single-user (token lungo, un device) |
| R3 | Prodotto per **uso personale**, non commerciale | Fuori scope tutto ciò che serve solo a vendere: multi-tenant, ruoli, form commerciali |
| R4 | **Inglese** come lingua primaria | UI, messaggi, nomi di endpoint, eventi e documentazione tecnica in EN. Questo piano resta in italiano perché è il documento di lavoro |

---

## 0. Sintesi esecutiva

Il backend di Alfredo è più avanti di quanto sembri: esiste già un punto di ingresso unico per
l'esecuzione dei run (`core/run_service.py`) e un sistema di approvazioni umane persistente e
riavviabile (`core/interrupts.py` + tabella `run_interrupts`). Quello che manca non è la logica,
è il **trasporto**: non esiste alcun canale realtime (zero WebSocket, zero SSE nel repo), l'API
espone solo trigger/poll dei workflow, e la logica conversazionale è intrappolata dentro `bot.py`.

Il piano costruisce quindi, nell'ordine:

1. un **event log** append-only come unica fonte di verità temporale;
2. una **API v1** completa (run, approvazioni, automazioni, sistema) + streaming;
3. un **servizio di chat minimale** (2A: conversazione libera + streaming), quanto basta
   perché l'app possa esistere come client;
4. un **accesso single-user** (un token, un device) — niente pairing cerimoniale;
5. una **PWA** installabile con push, che è la destinazione finale della chat;
6. **file/artefatti** e **gate GPU**;
7. la **parità di chat** e il passaggio di consegne Telegram → app;
8. hardening per l'uso reale dietro un tunnel.

Nota sulla sequenza: la chat non può passare all'app prima che l'app esista. Per questo la parte
difficile (macchina a stati conversazionale, planning, raccolta input, feedback) è **rinviata** alla
Fase 7, quando c'è già un client installabile su cui validarla.

Stima complessiva: **15–17 settimane part-time** (~15 h/settimana), **8–9 settimane full-time**.
Il primo traguardo utile (controllo remoto via API, senza UI) arriva in **2–3 settimane**; il
traguardo "chat sull'app, Telegram in fallback" intorno alla **settimana 14**.

---

## 1. Stato dell'arte verificato

Verificato leggendo il codice, non da documentazione.

| Componente | Stato | File |
|---|---|---|
| Punto unico di esecuzione run | **Presente** | `core/run_service.py` (`start_run`, `cancel_run`, `get_run`, `list_recent_runs`) |
| Approvazioni umane persistenti | **Presente** | `core/interrupts.py`, tabella `run_interrupts` (`core/sqlite_manager.py:243`) |
| Ripresa dopo approvazione / restart | **Presente** | `run_service.resume_interrupt`, `start_resume_interrupt` |
| Audit trail approvazioni | **Presente** | riga mantenuta con `status='answered'` |
| Hook di progresso a granularità task | **Presente** | `status_callback` in `execute_run`, invocato in `core/crew_builder.py:1316` |
| API REST | **Parziale** | `core/api_server.py` (health, workflow list, trigger, job status, app info) |
| Realtime (WS/SSE) | **Assente** | nessuna occorrenza nel repo: solo polling ogni 2 s |
| Auth per client | **Assente** | solo API key statica per app (`verify_app` in `core/api_server.py`) |
| Servizio di chat riutilizzabile | **Assente** | logica dentro `bot.py`, stato in `context.user_data` |
| Scheduler automazioni | **Parziale** | `core/automation_scheduler.py`: `config/automations.json`, tick 10 s |
| Catalogo capacità modelli | **Presente** | `core/model_capability.py` (tier + fallback + `prefer_local`) |
| SDK browser | **Presente** | `sdk/alfredo-sdk.js` (66 KB) — trigger/poll workflow |
| Registry pacchetti workflow | **Presente** | `core/hub_server.py` (utenti, token, visibilità) |

Conseguenza pratica: **la Fase 1 è breve perché il backend c'è**. Il costo vero è nelle Fasi 0 e 7.

---

## 2. Principi architetturali

1. **Il motore non conosce i client.** Nessun modulo in `core/` deve sapere che esiste Telegram,
   una PWA o un webhook. Oggi `human_in_the_loop.py` viola questo principio; `interrupts.py` no.
2. **Un solo log di eventi, N consumer.** Bot, app, push e webhook leggono la stessa sequenza.
   Lo streaming è una *tail* del log con `after_seq`, non un canale separato.
3. **Il PC è il titolare delle credenziali.** L'app non possiede VPN, intranet, share o token
   aziendali. È un telecomando cieco verso un agente su una macchina già autorizzata.
4. **Degrado esplicito, mai silenzioso.** Modello che non entra in VRAM, coda piena, run in
   attesa: si mostra lo stato, non si finge che sia tutto istantaneo.
5. **Il client primario cambia una volta sola, in modo esplicito.** Fino alla Fase 7 Telegram è
   il percorso primario e l'app il secondo client; dopo la Fase 7 l'app è primaria e Telegram
   resta come fallback operativo (e come rete di sicurezza se la PWA non è installata).

---

## 3. Debito tecnico bloccante

Da chiudere **prima della Fase 4** (non dopo: sono percorsi alternativi che si contendono lo
stesso run, e appena l'app mostra approvazioni la divergenza diventa un bug visibile).

### D1 — Doppio percorso HITL

`core/crew_builder.py` usa il nuovo interrupt solo se `allow_pause and run_id` (riga ~1452);
altrimenti ricade sul blocco legacy `request_human_input` (riga ~1495) e in un secondo punto
per il motore dinamico (riga ~2436). Effetto: un corpo batch/loop può restare appeso su Telegram
mentre l'app mostra il run come `waiting_human`, e l'approvazione presa dall'app non sblocca nulla.

- [ ] Rendere il percorso persistente l'unico per tutti i run con `run_id` valorizzato.
- [ ] Per i contesti senza `run_id`, aprire un run sintetico o rifiutare l'approvazione con
      errore esplicito — mai bloccare un thread per un'ora.
- [ ] Deprecare `core/human_in_the_loop.py` (resta solo come shim) e rimuovere `_hitl_lock`.
- [ ] Test: due approvazioni concorrenti su due run diversi, entrambe risolvibili.

### D2 — Le automazioni bypassano `run_service`

`automation_scheduler._trigger_workflow_async` (righe ~154–172) chiama direttamente
`execute_crew_with_memory`: nessun evento, nessun cancel, nessun interrupt, stato scritto a mano.

- [x] Delegare a `run_service.start_run(..., source="automation")`.
- [x] Propogare `rule_id` nel run (per la vista calendario e l'audit) — oggi nel payload
      dell'evento `automation.fired` sul run.

### D3 — Stato dello scheduler in RAM

`_last_run_ts` vive in memoria: al restart gli interval ripartono da zero e un trigger `daily`
che ha mancato il minuto è perso per la giornata.

- [x] Persistere `last_run_ts` e i file già visti in `config/automations_state.json`
      (scrittura atomica via file temporaneo + `os.replace`).
- [x] Catch-up esplicito per `interval` e `daily` entro una finestra configurabile
      (`catchup_minutes` per regola, default `ALFREDO_SCHEDULER_CATCHUP_MINUTES` = 720;
      oltre la finestra il trigger viene saltato e annotato in `last_skip`).
- [x] `update_rule` + editor nel dashboard (`✏️`) e calendario "Prossime esecuzioni (48h)"
      via `next_runs()`.
- [ ] (Opzionale) `watchdog` è in `requirements.txt` ma non usato: valutare sostituzione del
      polling `os.listdir` prima di esporre il folder-watch nell'app.

**Gate di uscita:** un run avviato da automazione è annullabile e visibile dall'app esattamente
come uno avviato a mano.

---

## 4. Fase 0 — Event log

**Obiettivo:** una sequenza di eventi per run, rileggibile e riproducibile.
**Stima:** 3–4 giorni. **Dipendenze:** nessuna.

### Lavoro

- [x] Tabella `run_events` in `core/sqlite_manager.py` e `core/postgres_manager.py`:
      `id INTEGER PK`, `run_id INTEGER`, `seq INTEGER`, `type TEXT`, `payload_json TEXT`,
      `created_at TIMESTAMP`, indice unico `(run_id, seq)`.
- [x] Nuovo modulo `core/events.py`: `emit(run_id, type, payload)`, `tail(after_seq, run_id=None, limit=200)`,
      `latest_seq(run_id)`, più `replay()` per ricostruire lo stato dallo stream.
- [x] `seq` per-run assegnato in modo atomico (`INSERT ... SELECT MAX(seq)+1` + retry su
      violazione dell'indice unico).
- [x] Emissione dai punti esistenti:
      - `run_service` → `run.started`, `run.paused`, `run.finished`, `run.failed`, `run.cancelled`;
      - `crew_builder` (entrambi i motori DAG) → `run.node.started/completed/failed`;
      - `interrupts.open_interrupt` / `resume_interrupt` → `run.interrupt.opened` / `.answered`;
      - `automation_scheduler` → `automation.fired`;
      - gate GPU (Fase 6) → `system.model.switch` (emesso dal gate quando il profilo cambia, dentro un run).
- [x] Retention: cap `ALFREDO_RUN_EVENTS_MAX` (default 5000) con prune lazy ogni 100 eventi.
- [x] Gli eventi **non** vengono emessi dentro `task_outputs_lock` di `crew_builder`.
- [x] Esposizione remota: `GET /api/runs/{id}/events?after_seq=N` (cursor `latest_seq`).

### Acceptance

- [ ] Dato un `run_id`, ricostruire stato, nodo corrente e approvazioni solo dagli eventi.
- [ ] `tail` con `after_seq` restituisce esattamente gli eventi mancanti, senza duplicati né buchi.
- [ ] Il bot si comporta in modo identico a prima (nessuna regressione funzionale).
- [ ] Il costo su un run da 20 nodi è < 5% del tempo di esecuzione.

---

## 5. Fase 1 — API v1

**Obiettivo:** controllo remoto completo senza Telegram.
**Stima:** 4–5 giorni. **Dipendenze:** Fase 0.

### Lavoro

- [ ] Router `/api/v1` separato (es. `core/api_app.py`) montato nell'app FastAPI esistente.
      `start_api_server` è già lanciato da `bot.py:2308` su `0.0.0.0:ALFREDO_API_PORT`: nessuno
      scaffolding nuovo.
- [ ] Autenticazione: riuso di `X-API-Key` per v1; il token di sessione single-user arriva in Fase 3.
- [ ] Endpoint (dettaglio in Appendice A).
- [ ] `WS /api/v1/stream`: messaggio di sottoscrizione `{topics: [...], after_seq: N}`,
      replay dal log, poi live. Heartbeat e chiusura pulita.
- [ ] Errori uniformi (`{code, message, retryable}`) e idempotency key su tutte le POST di comando.
- [ ] Rate limit minimo per IP e per token (l'app si riconnette, non deve martellare).

### Acceptance

- [ ] Da `curl`: avviare un workflow, leggere il progresso, approvare un interrupt, annullare il run.
- [ ] Riapertura del socket dopo disconnessione senza perdita di eventi (`after_seq`).
- [ ] Due client connessi ricevono gli stessi eventi.
- [ ] Nessuna modifica al comportamento del bot né del dashboard Streamlit.

**Milestone M1 — "telecomando via API".** Da qui il prodotto esiste anche senza interfaccia.

---

## 6. Fase 2 — Chat service, parte A (minimo indispensabile)

**Obiettivo:** un backend di chat che l'app possa consumare. Non la parità con Telegram.
**Stima:** 4–5 giorni. **Dipendenze:** Fasi 0–1. **Rischio: basso** (la parte rischiosa è in Fase 7).

### Perché solo la parte A

Serve una chat funzionante nell'app al più presto, ma la macchina a stati completa (planning,
raccolta input, feedback, contesto) è la cosa che più facilmente rompe il bot. Quindi si estrae
solo il percorso corto: conversazione libera, streaming e collegamento ai run. Il resto arriva
in Fase 7, quando esiste un client su cui testarlo.

### Lavoro

- [ ] `core/session_store.py`: tabella `sessions` (`id`, `kind`, `state_json`, `updated_at`) con
      API `load/save/clear`. Persistente, non in memoria: sopravvive al riavvio.
- [ ] `core/chat_service.py` con due sole responsabilità:
      - `handle_message(session_id, text) -> AsyncIterator[event]` per la **conversazione libera**
        (riuso di `MasterAI` come oggi fa `free_chat_handler`);
      - `run_from_message(session_id, workflow_id, inputs)` per avviare un workflow già esistente
        via `run_service.start_run`.
- [ ] Streaming token: variante streaming nel percorso chat di `core/master_ai.py`
      (oggi `stream=True` esiste solo per l'editor agenti/task, ~1457 e ~1609).
- [ ] Eventi di chat: `chat.token`, `chat.message`, `chat.error` (vedi Appendice B), emessi
      sull'event log della Fase 0 così da riusare la stessa tail WS.
- [ ] `POST /api/v1/chat` (non-streaming, per fallback) + topic WS `chat:{session_id}`.
- [ ] Nessuna modifica agli handler di `bot.py` in questa fase: il bot continua a funzionare
      esattamente come adesso, in parallelo.

### Acceptance

- [ ] Dalla sola API: creare una sessione, inviare un messaggio, ricevere token in streaming,
      riaprire il socket con `after_seq` e non perdere nulla.
- [ ] Riavviare il processo: la sessione e la cronologia recente sopravvivono.
- [ ] Il bot Telegram non cambia comportamento (nessuna regressione).

**Milestone M2 — l'app può autenticarsi e chattare in modalità libera.**

---

## 7. Fase 3 — Accesso single-user

**Obiettivo:** aprire l'app dal telefono in modo sicuro, senza cerimonie di pairing.
**Stima:** 2–3 giorni. **Dipendenze:** Fase 1. **Decisione R2.**

### Perché niente pairing

Il pairing con QR, chiavi pubbliche e revoca per device serve a gestire più dispositivi di più
persone. In un uso personale con un solo telefono è complessità che non ripaga: quello che serve
davvero è un segreto lungo, revocabile in un comando, e un modo semplice per non doverlo digitare.

### Lavoro

- [ ] Token di accesso lungo (32 byte random, base64url) generato dal comando
      `python -m core.auth issue-token --name iphone`, stampato una volta e salvato solo come hash.
- [ ] Tabella minimale `tokens`: `id`, `name`, `token_hash`, `created_at`, `last_seen`, `revoked_at`.
- [ ] `POST /api/v1/auth/exchange`: token → cookie di sessione httpOnly, SameSite=Strict, TTL 30 giorni.
      Il token non viene mai salvato in chiaro nel browser.
- [ ] Dipendenza `require_token` applicata a tutto `/api/v1` (esclusa `/auth/*` e `/health`).
- [ ] `python -m core.auth revoke --name iphone`: revoca immediata.
- [ ] Rate limit per IP su `/auth/exchange` e sulle POST di comando.
- [ ] Colonna `principal_id` (default `"local"`) su `workflow_runs` e `run_interrupts`: è una
      colonna, non un concetto nuovo — costa oggi e serve domani, ma non genera UI né ruoli.

### Acceptance

- [ ] L'app resta autenticata per 30 giorni senza reinserire nulla.
- [ ] Il token non è leggibile da JavaScript (cookie httpOnly) né compare nei log.
- [ ] `revoke` invalida l'accesso alla richiesta successiva.
- [ ] L'API key esistente dell'SDK continua a funzionare (compatibilità).

> Nota di sicurezza: con un tunnel pubblico il token lungo è accettabile **solo** perché è un
> singolo utente e il traffico è HTTPS. Se un domani l'app si apre ad altre persone, si torna
> alla Fase 3 originale (pairing, scope, revoca per device).

---

## 8. Fase 4 — Client PWA

**Obiettivo:** interfaccia installabile, streaming, approvazioni. È la destinazione finale della chat.
**Stima:** 3–4 settimane. **Dipendenze:** Fasi 1–3.

### Perché PWA e non Flutter

Il backend serve già asset statici, l'SDK browser esiste (`sdk/alfredo-sdk.js`), non serve
passare dagli store, gli aggiornamenti sono immediati. Flutter avrebbe senso per audio in
background, wearable o animazioni spinte: nessuno dei tre è nel percorso critico.

### Lavoro

- [ ] App servita su `/app` (build step minimo; riuso dei token visivi dell'SDK).
- [ ] Schermate: **Chat** (conversazione libera della Fase 2A, streaming, markdown, allegati),
      **Runs** (lista, dettaglio, timeline eventi), **Approvazioni** (action card con diff/preview
      e swipe-to-confirm), **Workflows**, **Automazioni** (calendario), **Sistema** (telemetria,
      coda GPU, sospensione).
- [ ] Chat come schermata di apertura: è il motivo per cui l'app viene installata.
- [ ] Todo lo user-facing in inglese (stringhe, date, formati numerici); lingua selezionabile in
      futuro ma non nella prima versione.
- [ ] Persistenza locale (IndexedDB) degli ultimi N eventi; resume da `after_seq`.
- [ ] Coda comandi offline con idempotency key: tre comandi in metro vengono eseguiti al
      reconnect una sola volta.
- [ ] Push WebPush (VAPID), payload minimo (`run_id`, tipo, titolo); dettagli fetchati al tap.
- [ ] Fallback push via `NotificationManager.send_telegram_notification` (già generalizzato con
      `callback_prefix`).

### Acceptance

- [ ] Installabile da home screen su iOS e Android.
- [ ] Approvazione end-to-end sotto 5 s (token → click → run riparte).
- [ ] Streaming chat fluido su rete 4G, senza flicker in riconnessione.
- [ ] Nessun comando duplicato dopo un reconnect.
- [ ] Funziona con il PC raggiungibile solo via tunnel.

**Milestone M3 — PWA installabile con notifiche.**

---

## 9. Fase 5 — File e artefatti — ✅ (parziale)

**Obiettivo:** consultare e condividere gli output senza scaricare 40 MB.
**Stima:** 5–6 giorni. **Dipendenze:** Fase 1 (autenticazione dalla Fase 3).

### Lavoro

- ✅ `GET /api/files?path=`: listing di `workspace/` con path jail in `core/files.py`
      (`clean_relative` + `resolve(strict=True)` due volte, quindi né `..` né symlink possono
      uscire dalla root; file nascosti, path assoluti, drive letter e NUL rifiutati).
- ✅ Preview server-side **senza eseguire codice utente**: xlsx/xlsm → tabella (openpyxl
      read-only), csv/tsv → tabella, testo/codice → testo, docx → paragrafi, PDF → pagine + testo
      (`pypdf`), immagini → thumbnail JPEG base64. Fallback `binary` con errore leggibile se la
      libreria manca o il file è corrotto.
- ✅ Download in streaming (`FileResponse`, nessun buffering) e URL firmati a TTL breve:
      HMAC-SHA256 stateless su `{path, expires_at}`, `POST /api/files/share` →
      `GET /api/files/shared/{token}` (endpoint pubblico, il token è la credenziale).
- ✅ `GET /api/runs/{run_id}/files`: risolve gli export dichiarati nel payload di un run dentro la
      jail (`describe_run_exports`, gestisce anche il resume con `restored_from`).
- ✅ Upload a chunk con checksum (`core/uploads.py`): session → un chunk per richiesta
      (`PUT .../chunks/{index}`, corpo `application/octet-stream`) → `complete` verifica
      dimensione + SHA-256 e sposta il file con `os.replace`. Le sessioni vivono in
      `workspace/.uploads/` (cartella nascosta, invisibile alla jail) e sono riprendibili dopo
      un reconnect/restart; i chunk rispediti sono idempotenti.
- ✅ PWA: nuova tab **File** (`mobile_web/src/screens/Files.tsx`) con breadcrumb, anteprime,
      download, link condivisi e upload multiplo con barra di avanzamento (`src/upload.ts`).
      Il client Python espone la stessa capacità con `upload_file` (resume via `session`).
- ⬜ Integrazione con `git_workspace` (modifiche di un run / rollback dall'app): **rimandata**,
      il rollback resta disponibile dal payload di resume.

### Acceptance

- ✅ Tentativi di traversal, path assoluti, symlink e drive letter rifiutati con 400
      (`FileAccessError` → HTTP 400, `FileNotFound` → 404).
- ✅ Preview xlsx/csv in memoria con righe/colonne limitate (default 30×12) e cap byte.
- ✅ Upload di 200 MB senza caricare l'intero file in RAM: un chunk per richiesta (default 1 MB,
      max 8 MB) e assemblaggio su disco con buffer fisso da 1 MB; cap totale configurabile
      (`ALFREDO_MAX_UPLOAD_MB`, default 512 MB), sessioni scadute pulite automaticamente.
- ✅ Link condiviso con TTL (default 1 h, minimo 60 s, massimo 7 giorni) e firma verificata con
      `hmac.compare_digest`; scaduto o manomesso → rifiutato.

**Test:** `tests/test_files.py` (29 casi: jail, preview per formato, export dei run, share token) e
`tests/test_uploads.py` (28 casi: path/size/chunk validation, ripresa, checksum, cleanup, jail).

---

## 10. Fase 6 — Gate GPU e routing — ✅ (con una riserva misurata)

**Obiettivo:** comportamento prevedibile su una GPU da 12 GB.
**Stima:** 3–4 giorni. **Dipendenze:** nessuna (ma va prima della Fase 4 "sistema").

### Vincoli misurati sul contesto reale

- Un 14B Q4 occupa ~8,5–9 GB: resta spazio per 8–16k di contesto con KV cache.
- Non è possibile tenere insieme LLM + embeddings + Whisper: embeddings su CPU,
  trascrizione **prima** del caricamento del modello, o thrash di VRAM e 20–30 s al primo token.
- `gpt-oss:20b` (in `config/models_map.yaml`) è al limite: spill su RAM, non va nel percorso critico.
- `deepseek-r1:14b` è un buon planner, mediocre come agente in loop (verboso, JSON instabile).

### Lavoro

- ✅ `core/gpu_gate.py`: lock **single-flight** + coda **FIFO** con timeout configurabile
  (`ALFREDO_GPU_QUEUE_TIMEOUT`, default 120 s), due profili (`fast` 7–8B Q4, `deep` 14B Q4)
  configurabili via env, riserva VRAM per il desktop (`ALFREDO_GPU_RESERVE_MB`, default 768 MB).
- ✅ Rifiuto esplicito se il profilo non entra: `GpuInsufficientVram` riporta VRAM richiesta,
  libera e un suggerimento azionabile (profilo alternativo che entra, liberare VRAM/ridurre il
  contesto). VRAM letta da `nvidia-smi` con cache di 3 s; GPU assente → nessun blocco, `fits: null`.
- ✅ Preload policy: al cambio profilo il modello viene scaldato in Ollama con `keep_alive`
  (`ALFREDO_GPU_PRELOAD`, `ALFREDO_GPU_KEEP_ALIVE`); i cambi sono contati come eviction.
- ✅ `MasterAI` usa il gate per **ogni** chiamata locale (era un semaforo che falliva subito):
  ora le richieste concorrenti aspettano il loro turno. Il run corrente arriva al gate tramite
  `core/run_context.py`, così l'app mostra "dietro run #42".
- ✅ Metriche: time-to-first-token per profilo (Ollama ora in **streaming**, stesso supporto
  JSON/schema), attesa media, durata media per profilo, chiamate servite/rifiutate/scadute,
  eviction, VRAM occupata/libera.
- ✅ `GET /api/system/status` (+ alias `/api/v1/system/status`): GPU, profili con `fits` e
  margine, coda (corrente + attesa stimata `eta_s`), metriche. Esposto in `mobile_client`
  (`system_status()`) e nella PWA: card "Sistema e GPU" in Impostazioni e banner in Chat
  quando c'è coda.

### Acceptance

- ⚠️ *Nessuna oscillazione di VRAM in un ciclo chat + embeddings + preview documento*:
  il gate elimina le chiamate LLM concorrenti (la causa principale di thrash) e non carica mai
  due profili insieme; gli **embeddings restano fuori dal gate** e vanno tenuti su CPU o su una
  seconda istanza Ollama come da vincolo. Non verificabile su questa macchina (nessuna GPU).
- ✅ La coda è visibile con profilo, run e **attesa stimata** (`eta_s` da durata media per
  profilo); niente spinner paralleli: il client mostra lo stato della coda.
- ✅ Latenza primo token misurata e registrata per profilo (`count/avg/p95/last`).

**Test:** `tests/test_gpu_gate.py` (16 casi: FIFO, timeout, fit check, preload/eviction,
metriche, run context, parsing `nvidia-smi`, wiring di `MasterAI`/API).

**Milestone M4 — prodotto completo sul percorso principale.**

---

## 10-bis. Dashboard allineato ai nuovi servizi (piano di controllo)

Il dashboard Streamlit resta il piano di controllo: ogni servizio introdotto nelle fasi 3–6 ha
un elemento che lo amministra, e ogni elemento della pagina chiama una funzione reale (nessun
bottone decorativo). Le modifiche di questo passaggio:

| Servizio | Capacità | Controllo nel dashboard |
|---|---|---|
| `run_service` | crea / esegue / annulla / riprende | tab **Run Workflows** (Assembler), **Run History** (🧾🔄🛑), **Assistant** |
| `events` | timeline per run, `after_seq` | **🧾 Event timeline** su ogni run + tab **Event log** in *Workspace & System* |
| `interrupts` | approvazioni persistenti | blocco **🙋 Pending approvals** in cima a *Run History* e *Run Workflows*, più l'Assistant |
| `automation_scheduler` | trigger, catch-up, anteprima 48h | **Triggers & Watchdog** (aggiunta, modifica, toggle, prossime esecuzioni) |
| `files` | browse, preview, download, share token | **Workspace & System → Workspace** |
| `files.describe_run_exports` | artefatti prodotti da un run | **Run History → 🧾 → 📦 Artifacts** (link firmato per file) |
| `uploads` | chunk resumable, TTL | **Workspace → Upload sessions & limits** (limiti + cleanup) |
| `gpu_gate` | single-flight, profili, coda, TTFT | **Workspace & System → GPU & Queue** |
| `push` | VAPID, subscription, test | **Workspace & System → Push** |
| `task_library` | template ↔ istanze | banner in **Task Builder**, pannello *Istanze e template* nell'Assembler |
| `workflow_package` | export/import `.alfredo.json`, compatibilità | tab **Share / Hub** + export pacchetto nelle workflow salvate |
| `git_workspace` | checkpoint e rollback | **Git Rollback** |
| `mcp_client`, `long_term_memory` | server MCP, fatti Mem0 | **MCP & Memory** |
| `assistant_engine` | chat libera + routing | pulsante 🤖 in alto a destra nella toolbar (dialog) |

Note di progetto:

- **Assistant**: non è più una pagina. È un pulsante rotondo con la faccia del robot, allineato a
  destra di *Start Bot Telegram* / *Telegram Bot Config*; apre un dialog con chat, workflow,
  approvazioni e run recenti. Le stringhe sono in inglese come il resto del prodotto (R4).
- **Input dei run**: il pannello non legge più le variabili con una regex sulle descrizioni. Usa
  `required_inputs` della task, le chiavi dei blocchi **Input** e i soli `inputs_map` con
  `from == "input"`. I placeholder di sistema e le porte cablate da un nodo padre non vengono
  chiesti. Rimossa la selectbox hardcoded `document_type` (valori di dominio di un singolo
  workflow) e il percorso diretto `execute_run_with_resume` + `db.update_run`.
- **`system.model.switch`**: ora emesso dal gate quando il profilo cambia dentro un run (sink
  installato da `run_service`), visibile nella timeline.
- **Fuori dal dashboard per scelta**: l'upload a chunk (`uploads`) resta client-side (app), il
  `node_executor` è motore puro, `run_context` è infrastruttura.

**Test:** `tests/test_dashboard_ui.py` (helper input, badge, contratto toolbar/pagine/servizi),
`tests/test_gpu_gate.py` (sink eventi).

---

## 11. Fase 7 — Parità di chat e passaggio di consegne

**Obiettivo:** spostare la chat da Telegram all'app, con Telegram in fallback. **Decisione R1.**
**Stima:** 2–3 settimane. **Dipendenze:** Fase 4 (app installata e usata), Fase 6, D1 chiuso.
**Rischio: alto.** È la fase che tocca il bot.

### Criterio di ingresso (gate)

Non si inizia finché **tutte** queste condizioni non sono vere:

- [ ] l'app è installata sullo smartphone e usata per almeno due settimane come client secondario;
- [ ] chat libera, run e approvazioni funzionano dall'app senza interventi sul PC;
- [ ] il doppio percorso HITL (D1) è chiuso: le approvazioni arrivano da qualunque client;
- [ ] non ci sono run rimasti bloccati in `waiting_human` negli ultimi 14 giorni;
- [ ] esiste un test end-to-end che copre il flusso di pianificazione su Telegram.

Se una di queste manca, la Fase 7 slitta: non c'è fretta, e il bot continua a funzionare.

### Lavoro

- [ ] Estendere `core/chat_service.py` con la macchina a stati completa, estratta da `bot.py`:
      `review_and_present_plan` (~737), `handle_planning_chat` (~998), `collect_required_inputs`
      (~1181), `handle_input_collection` (~1292), `execute_crew` (~1348). Circa 1.150 righe.
- [ ] `bot.py` diventa un adattatore sottile: update Telegram → `chat_service` → messaggi.
      Gli handler non contengono più logica conversazionale.
- [ ] Flag `ALFREDO_CHAT_ENGINE=service|legacy`, default `legacy` finché i test non passano.
- [ ] Parità funzionale verificata una funzione alla volta: menu workflow, piano, conferma,
      raccolta input, feedback, contesto ("continua / nuova sessione"), annullamento.
- [ ] L'app diventa il client primario: deep link `alfredo://` dai messaggi Telegram di fallback,
      e un messaggio esplicito quando un'approvazione è stata presa altrove.
- [ ] Telegram declassato a fallback: notifiche e comandi rapidi, non più il percorso di planning.

### Acceptance

- [ ] Lo stesso scenario (workflow con validazione HITL) completa sia da app sia da Telegram.
- [ ] Una sessione iniziata su un client è conclusa sull'altro senza perdita di stato.
- [ ] Approvazione presa in app → il messaggio Telegram corrispondente si chiude, e viceversa.
- [ ] Nessuna regressione sui test esistenti (`tests/`), incluso `test_interrupts.py`.
- [ ] Rollback in un comando (flag a `legacy`) senza perdita di sessioni.

**Milestone M5 — chat sull'app, Telegram in fallback.** Da qui il prodotto è "finito" per uso personale.

---

## 12. Fase 8 — Hardening

**Obiettivo:** sopravvivere all'uso quotidiano dietro un tunnel, senza sorprese.
**Stima:** 1–2 settimane. **Dipendenze:** tutte.

- [ ] Egress allowlist per workflow; ogni chiamata outbound loggata con `run_id` + `principal_id`.
- [ ] Tool deny-by-default per shell e scritture fuori da `workspace/` (la blacklist attuale in
      `tools/terminal_executor.py` **non** è un confine di sicurezza).
- [ ] Budget per run: max step, secondi, token. Superamento → pausa con approvazione.
- [ ] Prompt injection: i contenuti di `crawl4ai`, email e PDF sono input non fidati per un agente
      con `execute_shell_command`. Trattamento come dati, non istruzioni; nessuna esecuzione
      automatica di ciò che arriva dal web.
- [ ] `alfredo doctor`: RAM/VRAM, Ollama raggiungibile, modelli presenti, permessi cartelle,
      porta API, stato tunnel, spazio disco.
- [ ] Backup/restore della configurazione del nodo (`config/automations.json`, DB, workspace) e
      migrazioni versionate.
- [ ] Script di setup tunnel (Cloudflare Tunnel) con verifica automatica.
- [ ] Log applicativi in ring buffer consultabili dall'app (ultimi N eventi di errore).

---

## 13. Calendario e milestone

Assunzione: una persona sola, ~15 h/settimana.

| Milestone | Contenuto | Settimane | Rischio |
|---|---|---|---|
| D | Debito D1–D3 chiuso | 1 | medio |
| M1 | Fasi 0–1: telecomando via API | 1–3 | basso |
| M2 | Fasi 2–3: chat libera + accesso single-user | 4–5 | basso |
| M3 | Fase 4: PWA installabile + push + approvazioni | 6–9 | medio |
| M4 | Fasi 5–6: file, telemetria, gate GPU | 10–11 | basso |
| M5 | Fase 7: parità chat + switch a client primario | 12–14 | **alto** |
| M6 | Fase 8: hardening + installer + tunnel | 15–16 | medio |

Full-time (40 h/settimana): M1–M5 in 8 settimane, M6 in 9.

Ordine non negoziabile: **M5 dopo M3**, perché il passaggio di consegne richiede un client che
esista e che sia stato usato. Chi inverte l'ordine si ritrova a riscrivere `bot.py` per un'app
che non ha ancora nessuno che la usi.

---

## 14. Rischi e criteri di stop

| Rischio | Segnale | Mitigazione |
|---|---|---|
| Regressione bot durante la Fase 7 | flussi Telegram rotti | flag `ALFREDO_CHAT_ENGINE`, gate di ingresso, rollback in un comando |
| Ho due HITL che si contendono i run | run bloccati su `waiting_human` senza approvazione | chiudere D1 **prima** della Fase 4 (prima che l'app mostri approvazioni) |
| La Fase 7 si allarga (memoria, contesto, feedback, feature nuove) | settimane che slittano | limite: solo i flussi già in `bot.py`, nessuna feature nuova |
| Chat sull'app non ancora all'altezza | l'utente continua a usare Telegram | è accettabile: il gate di ingresso esiste proprio per questo |
| Doppio client con stati divergenti | stesso run visto diversamente | un solo event log come verità; nessuno stato locale autorevole |
| 12 GB di VRAM | thrash, primo token a 30 s | gate GPU, due profili, embeddings su CPU |
| Supporto a se stessi (futuro: utenti) | "non funziona" senza diagnostica | `doctor` + bundle diagnostico |
| Tunnel perceived as "E2E encrypted" | promessa errata | dire "TLS fino all'edge + payload cifrato opzionale", mai "end-to-end" |
| WoL | PC non si sveglia da Internet | qualificare: serve RTC/BIOS, device sempre acceso, o port forward |

**Criteri di stop (da rivedere prima di investire nella Fase 4):**

- se il tool-calling su 14B fallisce oltre il 15–20% sui workflow reali, la promessa "locale"
  non regge: l'orchestrator cloud diventa default e cambia il pitch, non il codice;
- se l'approvazione end-to-end non sta sotto ~5 s, l'utente la disabilita e l'agente diventa
  pericoloso;
- se l'installazione su Windows pulito non è affidabile in una sera, ogni reinstallazione diventa
  un progetto: per uso personale è tollerabile una volta, non ogni mese.

---

## 15. Fuori scope (per ora)

Coerenti con le decisioni R2 e R3 (uso personale, un solo device):

- **pairing multi-device** (QR, chiavi pubbliche, scope, revoca per device);
- **multi-tenant**, ruoli e permessi;
- **track commerciale**: licenza, setup assistito, marketplace, onboarding di terzi;
- Flutter e app native; relay proprietario; DAG visuale in tempo reale; voce
  (`faster-whisper` + Piper/Kokoro); fine-tuning locale; Wake-on-LAN come feature di punta.

Motivo: nessuno è nel percorso critico M1–M5, e ognuno aggiunge superficie di supporto.
Voce, DAG e pairing sono ottimi candidati per una v2 **dopo** che il percorso principale è
stabile e la chat è stabilmente sull'app.

---

## Appendice A — Superficie API v1

| Metodo | Path | Note |
|---|---|---|
| GET | `/api/v1/runs` | `limit`, `status`, `source` |
| GET | `/api/v1/runs/{id}` | `run_service.parse_run_record` |
| POST | `/api/v1/runs/{id}/cancel` | `run_service.cancel_run` |
| GET | `/api/v1/runs/{id}/events` | `after_seq`, `limit` |
| POST | `/api/v1/workflows/{id}/run` | `run_service.start_run` |
| GET | `/api/v1/workflows` | riuso `_get_workflow_required_inputs` |
| GET | `/api/v1/interrupts` | `status=pending`, `run_id` |
| POST | `/api/v1/interrupts/{id}/answer` | `start_resume_interrupt`, idempotente |
| GET/POST/PATCH/DELETE | `/api/v1/automations` | richiede `update_rule` (D3) |
| GET | `/api/v1/system/status` | CPU/GPU/VRAM, coda, versione, uptime |
| POST | `/api/v1/system/suspend` | richiede autenticazione (single-user) |
| GET | `/api/v1/files` | listing `workspace/` con path jail |
| POST | `/api/v1/files/upload` | chunk + checksum |
| POST | `/api/v1/auth/exchange` | token → cookie httpOnly (Fase 3) |
| POST | `/api/v1/chat` | conversazione libera (Fase 2A); parità completa in Fase 7 |
| WS | `/api/v1/stream` | topic: `run:{id}`, `chat:{session}`, `approvals`, `automations`, `system` |

Convenzioni: errori `{code, message, retryable}`; `Idempotency-Key` obbligatoria sulle POST di
comando; `X-API-Key` per compatibilità SDK, cookie di sessione (`/auth/exchange`) per l'app.
Tutti i payload, i nomi dei campi e i messaggi di errore in **inglese** (decisione R4).

## Appendice B — Tipi di evento

| Tipo | Payload minimo |
|---|---|
| `run.started` | `workflow_id`, `source`, `inputs_keys` |
| `run.node.started` | `task_idx`, `total`, `agent_role`, `model` |
| `run.node.completed` | `task_idx`, `duration_ms`, `output_chars` |
| `run.interrupt.opened` | `interrupt_id`, `node_id`, `question`, `options` |
| `run.interrupt.answered` | `interrupt_id`, `answer`, `by` (principal/device) |
| `run.paused` | `reason` (`hitl` \| `rate_limit` \| `budget`) |
| `run.finished` | `status`, `exports`, `duration_ms` |
| `automation.fired` | `rule_id`, `run_id`, `trigger` |
| `system.model.switch` | `profile`, `model`, `estimated_vram_mb` |
| `chat.token` | `session_id`, `delta` (streaming) |
| `chat.message` | `session_id`, `role`, `text`, `run_id?` |
| `chat.error` | `session_id`, `code`, `retryable` |

## Appendice C — Schema nuove tabelle

```
run_events(id PK, run_id, seq, type, payload_json, created_at)  -- UNIQUE(run_id, seq)
sessions(id PK, kind, state_json, updated_at)
tokens(id PK, name, token_hash, created_at, last_seen, revoked_at)   -- Fase 3, single-user
```

`principal_id` (default `"local"`) da aggiungere come colonna a `workflow_runs` e `run_interrupts`.
Nessuna tabella `devices` in questo piano: il pairing multi-device è fuori scope (R2).

## Appendice D — Decisioni aperte

1. PWA (raccomandata) vs app nativa: confermare prima della Fase 4.
2. Se il desktop possa restare acceso 24/7 o serva RTC/device relay: incide sui costi reali, non
   sulla pianificazione.
3. ~~Forma commerciale~~ — deciso: uso personale (R3), nessun lavoro in questa direzione.
4. ~~Lingua~~ — deciso: inglese come lingua primaria del prodotto (R4).
5. **Nuova:** quando girare il gate della Fase 7. Criterio proposto: due settimane di uso reale
   dell'app come client secondario, senza run bloccati. Se dopo due settimane usi ancora Telegram
   per chattare, il problema è l'app, non la chat.
