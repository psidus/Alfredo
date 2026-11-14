import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useLanguage } from "../i18n";
import type { ConnectionConfig } from "../types";

const MAX_CACHED_AUDIO = 50;
const audioUrlCache = new Map<string, string>();
let activeStopFn: (() => void) | null = null;

function cacheAudioUrl(key: string, url: string): void {
  if (audioUrlCache.has(key)) return;
  if (audioUrlCache.size >= MAX_CACHED_AUDIO) {
    const oldestKey = audioUrlCache.keys().next().value;
    if (oldestKey !== undefined) {
      const oldestUrl = audioUrlCache.get(oldestKey);
      if (oldestUrl) URL.revokeObjectURL(oldestUrl);
      audioUrlCache.delete(oldestKey);
    }
  }
  audioUrlCache.set(key, url);
}

function pickRecorderMime(): { mimeType: string; filename: string } {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") {
    return { mimeType: "", filename: "voice.webm" };
  }
  const candidates: Array<{ mime: string; ext: string }> = [
    { mime: "audio/webm;codecs=opus", ext: "webm" },
    { mime: "audio/webm", ext: "webm" },
    { mime: "audio/mp4", ext: "m4a" },
    { mime: "audio/ogg;codecs=opus", ext: "ogg" },
    { mime: "audio/ogg", ext: "ogg" },
  ];
  for (const item of candidates) {
    if (MediaRecorder.isTypeSupported(item.mime)) {
      return { mimeType: item.mime, filename: `voice.${item.ext}` };
    }
  }
  return { mimeType: "", filename: "voice.webm" };
}

function formatTimer(seconds: number): string {
  const mins = Math.floor(seconds / 60);
  const secs = seconds % 60;
  return `${mins}:${secs.toString().padStart(2, "0")}`;
}

export interface VoiceRecorderProps {
  config: ConnectionConfig;
  disabled?: boolean;
  onTranscribeToDraft: (text: string) => void;
  onSendDirect?: (text: string, durationS: number) => void;
  onError?: (message: string) => void;
  onActiveChange?: (active: boolean) => void;
}

export function VoiceRecorderButton({
  config,
  disabled,
  onTranscribeToDraft,
  onSendDirect,
  onError,
  onActiveChange,
}: VoiceRecorderProps) {
  const { lang, t } = useLanguage();
  const [phase, setPhase] = useState<"idle" | "recording" | "transcribing">("idle");
  const [elapsed, setElapsed] = useState(0);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const filenameRef = useRef("voice.webm");
  const mimeRef = useRef("audio/webm");
  const elapsedRef = useRef(0);

  const setPhaseSync = useCallback(
    (next: "idle" | "recording" | "transcribing") => {
      setPhase(next);
      onActiveChange?.(next !== "idle");
    },
    [onActiveChange],
  );

  const cleanupMedia = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((track) => track.stop());
      streamRef.current = null;
    }
    recorderRef.current = null;
  }, []);

  useEffect(() => {
    return () => {
      cleanupMedia();
    };
  }, [cleanupMedia]);

  const startRecording = async () => {
    if (disabled || phase !== "idle") return;
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      onError?.(t("micDeniedError"));
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      chunksRef.current = [];

      const picked = pickRecorderMime();
      filenameRef.current = picked.filename;
      const recorder = picked.mimeType
        ? new MediaRecorder(stream, { mimeType: picked.mimeType })
        : new MediaRecorder(stream);

      mimeRef.current = recorder.mimeType || picked.mimeType || "audio/webm";
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorderRef.current = recorder;
      recorder.start(250);
      elapsedRef.current = 0;
      setElapsed(0);
      setPhaseSync("recording");

      timerRef.current = window.setInterval(() => {
        elapsedRef.current += 1;
        setElapsed(elapsedRef.current);
      }, 1000);
    } catch {
      cleanupMedia();
      onError?.(t("micDeniedError"));
    }
  };

  const stopAndCollectBlob = (): Promise<Blob> => {
    return new Promise((resolve) => {
      const recorder = recorderRef.current;
      if (!recorder || recorder.state === "inactive") {
        cleanupMedia();
        resolve(new Blob(chunksRef.current, { type: mimeRef.current }));
        return;
      }
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mimeRef.current });
        chunksRef.current = [];
        cleanupMedia();
        resolve(blob);
      };
      recorder.stop();
    });
  };

  const cancelRecording = () => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = null;
      recorder.stop();
    }
    chunksRef.current = [];
    cleanupMedia();
    setElapsed(0);
    setPhaseSync("idle");
  };

  const finishRecording = async (mode: "draft" | "direct") => {
    if (phase !== "recording") return;
    const durationSnapshot = elapsedRef.current;
    setPhaseSync("transcribing");
    try {
      const blob = await stopAndCollectBlob();
      if (!blob || blob.size === 0) {
        setPhaseSync("idle");
        return;
      }
      const res = await api.transcribeAudio(config, blob, lang, filenameRef.current);
      const text = (res.text || "").trim();
      if (!text) {
        onError?.(lang === "en" ? "No speech detected in audio." : "Nessun parlato rilevato nel vocale.");
        return;
      }
      if (mode === "direct" && onSendDirect) {
        onSendDirect(text, res.duration_s || durationSnapshot);
      } else {
        onTranscribeToDraft(text);
      }
    } catch (exc) {
      onError?.(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setElapsed(0);
      setPhaseSync("idle");
    }
  };

  if (phase === "idle") {
    return (
      <button
        type="button"
        className="btn-mic"
        onClick={() => void startRecording()}
        disabled={disabled}
        title={t("recordVoice")}
        aria-label={t("recordVoice")}
      >
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="9" y="2" width="6" height="11" rx="3" />
          <path d="M5 10v2a7 7 0 0 0 14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="22" />
          <line x1="8" y1="22" x2="16" y2="22" />
        </svg>
      </button>
    );
  }

  return (
    <div className="voice-recorder-bar">
      <div className="voice-status">
        <span className={phase === "recording" ? "voice-dot recording" : "voice-dot"} />
        <span className="voice-timer">
          {phase === "transcribing" ? t("transcribingVoice") : formatTimer(elapsed)}
        </span>
      </div>

      {phase === "recording" ? (
        <div className="voice-actions">
          <button
            type="button"
            className="btn small ghost"
            onClick={cancelRecording}
            title={t("cancelVoice")}
          >
            🗑️
          </button>
          <button
            type="button"
            className="btn small"
            onClick={() => void finishRecording("draft")}
            title={t("transcribeToText")}
          >
            ✏️ {t("transcribeToText")}
          </button>
          {onSendDirect ? (
            <button
              type="button"
              className="btn small primary"
              onClick={() => void finishRecording("direct")}
              title={t("sendVoiceNow")}
            >
              ✈️ {t("sendVoiceNow")}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

const SPEED_OPTIONS = [1, 1.25, 1.5] as const;

export interface VoicePlayerProps {
  config: ConnectionConfig;
  text: string;
  autoPlay?: boolean;
  onAutoPlayed?: () => void;
  onError?: (message: string) => void;
}

export function VoicePlayerButton({
  config,
  text,
  autoPlay = false,
  onAutoPlayed,
  onError,
}: VoicePlayerProps) {
  const { lang, t } = useLanguage();
  const [status, setStatus] = useState<"idle" | "loading" | "playing" | "paused">("idle");
  const [speedIdx, setSpeedIdx] = useState(0);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const autoTriggeredRef = useRef(false);

  const speed = SPEED_OPTIONS[speedIdx];

  const stopPlayback = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
    }
    setStatus("idle");
  }, []);

  useEffect(() => {
    return () => {
      if (audioRef.current) {
        audioRef.current.pause();
        audioRef.current = null;
      }
    };
  }, []);

  const startPlayback = useCallback(
    async (silentAutoPlayFailure = false) => {
      const clean = (text || "").trim();
      if (!clean) return;

      if (status === "playing" && audioRef.current) {
        audioRef.current.pause();
        setStatus("paused");
        return;
      }

      if (status === "paused" && audioRef.current) {
        if (activeStopFn && activeStopFn !== stopPlayback) activeStopFn();
        activeStopFn = stopPlayback;
        audioRef.current.playbackRate = speed;
        try {
          await audioRef.current.play();
          setStatus("playing");
        } catch (exc) {
          if (!silentAutoPlayFailure) {
            onError?.(exc instanceof Error ? exc.message : String(exc));
          }
          setStatus("idle");
        }
        return;
      }

      if (activeStopFn && activeStopFn !== stopPlayback) {
        activeStopFn();
      }
      activeStopFn = stopPlayback;

      const cacheKey = `${lang}:${clean}`;
      let url = audioUrlCache.get(cacheKey);

      if (!url) {
        setStatus("loading");
        try {
          const blob = await api.synthesizeSpeech(config, clean, lang, 1.0);
          url = URL.createObjectURL(blob);
          cacheAudioUrl(cacheKey, url);
        } catch (exc) {
          setStatus("idle");
          if (!silentAutoPlayFailure) {
            onError?.(exc instanceof ApiError ? exc.message : String(exc));
          }
          return;
        }
      }

      const audio = new Audio(url);
      audio.playbackRate = speed;
      audio.onended = () => {
        setStatus("idle");
        if (activeStopFn === stopPlayback) activeStopFn = null;
      };
      audio.onerror = () => {
        setStatus("idle");
      };
      audioRef.current = audio;

      try {
        await audio.play();
        setStatus("playing");
      } catch (exc) {
        setStatus("idle");
        if (!silentAutoPlayFailure) {
          onError?.(exc instanceof Error ? exc.message : String(exc));
        }
      }
    },
    [config, lang, onError, speed, status, stopPlayback, text],
  );

  useEffect(() => {
    if (autoPlay && !autoTriggeredRef.current && text.trim()) {
      autoTriggeredRef.current = true;
      onAutoPlayed?.();
      void startPlayback(true);
    }
  }, [autoPlay, onAutoPlayed, startPlayback, text]);

  const cycleSpeed = () => {
    const nextIdx = (speedIdx + 1) % SPEED_OPTIONS.length;
    setSpeedIdx(nextIdx);
    if (audioRef.current) {
      audioRef.current.playbackRate = SPEED_OPTIONS[nextIdx];
    }
  };

  return (
    <div className="voice-player-row">
      <button
        type="button"
        className="btn-voice-listen"
        onClick={() => void startPlayback(false)}
        disabled={status === "loading"}
      >
        {status === "loading" ? (
          <>⏳ {t("loadingAudio")}</>
        ) : status === "playing" ? (
          <>⏸️ {t("pauseAudio")}</>
        ) : status === "paused" ? (
          <>▶️ {t("resumeAudio")}</>
        ) : (
          <>🔊 {t("listenReply")}</>
        )}
      </button>

      {status === "playing" || status === "paused" ? (
        <>
          <button
            type="button"
            className="btn-voice-listen"
            onClick={stopPlayback}
            title={t("stopAudio")}
          >
            ⏹️
          </button>
          <button
            type="button"
            className="btn-voice-speed"
            onClick={cycleSpeed}
            title="Playback speed"
          >
            {speed}x
          </button>
        </>
      ) : null}
    </div>
  );
}
