import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("Root element not found");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Register the offline shell only for production builds (Vite dev serves modules
// that must never be cached by a service worker).
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  const registerSw = () => {
    const swUrl = `${import.meta.env.BASE_URL}sw.js`;
    navigator.serviceWorker
      .register(swUrl, { scope: import.meta.env.BASE_URL })
      .then((reg) => {
        console.log("[PWA] Service Worker registrato con successo. Scope:", reg.scope);
      })
      .catch((err) => {
        console.warn("[PWA] Registrazione Service Worker fallita:", err);
      });
  };

  if (document.readyState === "complete") {
    registerSw();
  } else {
    window.addEventListener("load", registerSw);
  }
}

// Intercetta l'evento di installazione PWA per permettere l'installazione guidata
window.addEventListener("beforeinstallprompt", (e) => {
  console.log("[PWA] Evento beforeinstallprompt catturato!");
  (window as unknown as { deferredInstallPrompt?: Event }).deferredInstallPrompt = e;
});
