import { api } from "./api";
import type { ConnectionConfig } from "./types";

const PUSH_ENDPOINT_KEY = "alfredo.push.endpoint.v1";

export interface PushStatus {
  supported: boolean;
  permission: NotificationPermission | "unsupported";
  subscribed: boolean;
  endpoint: string | null;
}

function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const normalized = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(normalized);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

async function registrationOrThrow(): Promise<ServiceWorkerRegistration> {
  const scope = import.meta.env.BASE_URL;
  const existing = await navigator.serviceWorker.getRegistration(scope);
  if (existing?.active) return existing;

  // In dev the service worker is intentionally not registered: fail fast instead
  // of hanging forever on navigator.serviceWorker.ready.
  const ready = navigator.serviceWorker.ready;
  const timeout = new Promise<never>((_, reject) => {
    window.setTimeout(
      () =>
        reject(
          new Error(
            "Service worker non attivo. Apri Alfredo dalla versione servita dal server (/app/), non in modalità dev.",
          ),
        ),
      4000,
    );
  });
  return Promise.race([ready, timeout]);
}

export async function pushStatus(): Promise<PushStatus> {
  if (!pushSupported()) {
    return { supported: false, permission: "unsupported", subscribed: false, endpoint: null };
  }
  let endpoint: string | null = null;
  try {
    const registration = await navigator.serviceWorker.getRegistration(import.meta.env.BASE_URL);
    const subscription = await registration?.pushManager.getSubscription();
    endpoint = subscription?.endpoint ?? null;
  } catch {
    endpoint = null;
  }
  if (!endpoint) endpoint = localStorage.getItem(PUSH_ENDPOINT_KEY);
  return {
    supported: true,
    permission: Notification.permission,
    subscribed: Boolean(endpoint) && Notification.permission === "granted",
    endpoint,
  };
}

export async function enablePush(config: ConnectionConfig): Promise<string> {
  if (!pushSupported()) {
    throw new Error("Questo browser non supporta le notifiche push.");
  }

  const key = await api.pushKey(config);
  if (!key.enabled) {
    throw new Error(
      "Il server non può inviare notifiche: installa pywebpush (pip install pywebpush) e riavvia Alfredo.",
    );
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error("Permesso notifiche negato: abilitalo dalle impostazioni del browser/sito.");
  }

  const registration = await registrationOrThrow();
  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    try {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key.public_key) as BufferSource,
      });
    } catch (error) {
      throw new Error(
        `Sottoscrizione push rifiutata dal browser: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  const json = subscription.toJSON();
  const p256dh = json.keys?.p256dh ?? "";
  const auth = json.keys?.auth ?? "";
  if (!p256dh || !auth) {
    throw new Error("Il browser non ha restituito le chiavi della sottoscrizione push.");
  }

  await api.pushSubscribe(config, {
    endpoint: subscription.endpoint,
    keys: { p256dh, auth },
    user_agent: navigator.userAgent.slice(0, 200),
  });
  localStorage.setItem(PUSH_ENDPOINT_KEY, subscription.endpoint);
  return subscription.endpoint;
}

export async function disablePush(config: ConnectionConfig): Promise<void> {
  let endpoint = localStorage.getItem(PUSH_ENDPOINT_KEY);
  try {
    const registration = await navigator.serviceWorker.getRegistration(import.meta.env.BASE_URL);
    const subscription = await registration?.pushManager.getSubscription();
    if (subscription) {
      endpoint = subscription.endpoint;
      await subscription.unsubscribe();
    }
  } catch {
    // Fall back to the stored endpoint; the server can still drop the row.
  }
  if (endpoint) {
    try {
      await api.pushUnsubscribe(config, endpoint);
    } catch {
      // The row may already be gone (410 cleanup on the server).
    }
  }
  localStorage.removeItem(PUSH_ENDPOINT_KEY);
}

export async function sendTestPush(config: ConnectionConfig): Promise<number> {
  const result = await api.pushTest(config);
  return result.delivered;
}
