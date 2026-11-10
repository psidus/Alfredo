import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { uploadBrowserFile, type UploadProgress } from "../upload";
import type {
  ConnectionConfig,
  FilePreview,
  RunFile,
  ShareLink,
} from "../types";
import { ErrorBanner, Spinner, formatBytes, usePolling } from "../components/ui";
import { FilePreviewCard } from "../components/FilePreviewCard";
import { useLanguage, type Language } from "../i18n";

interface Props {
  config: ConnectionConfig;
  lang?: Language;
}

const KIND_ICONS: Record<string, string> = {
  folder: "📁",
  image: "🖼️",
  pdf: "📄",
  table: "📊",
  text: "📝",
  archive: "🗜️",
  doc: "📃",
  binary: "📦",
};

function iconFor(entry: RunFile): string {
  return KIND_ICONS[entry.is_dir ? "folder" : entry.kind ?? "binary"] ?? "📦";
}

function parentOf(path: string): string {
  const parts = path.split("/").filter(Boolean);
  parts.pop();
  return parts.join("/");
}

export function FilesScreen({ config, lang: propLang }: Props) {
  const context = useLanguage();
  const currentLang = propLang || context.lang || "it";
  const t = context.t;

  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<RunFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [share, setShare] = useState<ShareLink | null>(null);
  const [upload, setUpload] = useState<UploadProgress | null>(null);
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);

  const load = useCallback(
    async (targetPath: string) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setLoading(true);
      setError(null);
      try {
        const listing = await api.listFiles(config, targetPath);
        setEntries(listing.entries ?? []);
      } catch (exc) {
        setError(exc instanceof ApiError ? exc.message : String(exc));
      } finally {
        setLoading(false);
        inFlight.current = false;
      }
    },
    [config],
  );

  useEffect(() => {
    void load(path);
  }, [path, load]);

  usePolling(() => void load(path), 12000, Boolean(config.baseUrl));

  const uploadFiles = async (files: File[]) => {
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      for (const file of files) {
        await uploadBrowserFile(config, file, path, (p) => setUpload(p));
      }
      setUpload(null);
      await load(path);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setUpload(null);
      setBusy(false);
    }
  };

  const openPreview = async (entry: RunFile) => {
    if (entry.is_dir) {
      setPath(entry.path);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setPreview(await api.filePreview(config, entry.path));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const download = async (entry: RunFile) => {
    setBusy(true);
    setError(null);
    try {
      const blob = await api.downloadFile(config, entry.path);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = entry.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const createShare = async (entry: RunFile) => {
    setBusy(true);
    setError(null);
    try {
      setShare(await api.shareFile(config, entry.path, 24 * 3600));
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (entry: RunFile) => {
    const promptMsg = entry.is_dir ? t("confirmDeleteFolder") : t("confirmDeleteFile");
    if (!window.confirm(promptMsg)) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteFile(config, entry.path);
      await load(path);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const segments = path.split("/").filter(Boolean);
  const folderName = segments.length ? segments[segments.length - 1] : "Workspace";

  return (
    <div className="screen">
      <div className="row between">
        <button
          className="btn small ghost btn-nav-back"
          onClick={() => setPath(parentOf(path))}
          disabled={!path}
          title={currentLang === "en" ? "Parent folder" : "Cartella superiore"}
        >
          <svg
            className="arrow-left-svg"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M10 13L5 8L10 3" />
          </svg>
          <span className="nav-text">{currentLang === "en" ? "Up" : "Su"}</span>
        </button>
        <button
          className="btn small ghost"
          onClick={() => void load(path)}
          disabled={loading}
          aria-label={t("refresh")}
        >
          ↻
        </button>
      </div>

      <ErrorBanner message={error} onDismiss={() => setError(null)} />

      <div className="scroll-area">
        <div className="card">
          <div className="row between">
            <div>
              <h2>{folderName}</h2>
              <p className="muted">
                {currentLang === "en"
                  ? "Files produced by workflows and uploaded here."
                  : "File prodotti dai workflow e caricati da qui."}
              </p>
            </div>
            <button
              className="btn small primary"
              onClick={() => inputRef.current?.click()}
              disabled={busy}
            >
              {currentLang === "en" ? "Upload here" : "Carica qui"}
            </button>
          </div>
          <input
            ref={inputRef}
            type="file"
            multiple
            hidden
            onChange={(event) => void uploadFiles(Array.from(event.target.files ?? []))}
          />
          <div className="breadcrumb">
            <button className="btn small ghost" onClick={() => setPath("")}>
              workspace
            </button>
            {segments.map((segment, index) => (
              <span key={`${segment}-${index}`}>
                <span className="muted">/</span>
                <button
                  className="btn small ghost"
                  onClick={() => setPath(segments.slice(0, index + 1).join("/"))}
                >
                  {segment}
                </button>
              </span>
            ))}
          </div>
          {upload ? (
            <div className="upload-bar">
              <span className="muted">
                {upload.name} · {formatBytes(upload.sent)} / {formatBytes(upload.total)}
              </span>
              <div className="upload-track">
                <div
                  className="upload-fill"
                  style={{
                    width: `${upload.total ? Math.round((upload.sent / upload.total) * 100) : 0}%`,
                  }}
                />
              </div>
            </div>
          ) : null}
        </div>

        <div className="card">
          <div className="row between">
            <h3>{folderName}</h3>
            <span className="muted">
              {loading
                ? t("loading")
                : `${entries.length} ${currentLang === "en" ? (entries.length === 1 ? "item" : "items") : "elementi"}`}
            </span>
          </div>
          {loading && entries.length === 0 ? (
            <div className="empty row" style={{ justifyContent: "center" }}>
              <Spinner /> {t("loading")}
            </div>
          ) : entries.length === 0 ? (
            <p className="muted">{t("emptyFolder")}</p>
          ) : (
            entries.map((entry) => (
              <div className="file-row" key={entry.path || entry.name}>
                <div className="file-meta">
                  <strong>
                    {iconFor(entry)} {entry.name}
                  </strong>
                  <span className="muted">
                    {entry.is_dir ? (currentLang === "en" ? "folder" : "cartella") : entry.kind}
                    {!entry.is_dir && entry.size != null ? ` · ${formatBytes(entry.size)}` : ""}
                    {entry.modified ? ` · ${entry.modified}` : ""}
                  </span>
                </div>
                <div className="actions">
                  <button
                    className="btn small"
                    onClick={() => void openPreview(entry)}
                    disabled={busy}
                  >
                    {entry.is_dir ? (currentLang === "en" ? "Open" : "Apri") : t("preview")}
                  </button>
                  {!entry.is_dir ? (
                    <>
                      <button
                        className="btn small"
                        onClick={() => void download(entry)}
                        disabled={busy}
                      >
                        {t("download")}
                      </button>
                      <button
                        className="btn small ghost"
                        onClick={() => void createShare(entry)}
                        disabled={busy}
                      >
                        {t("shareLink")}
                      </button>
                    </>
                  ) : null}
                  <button
                    className="btn-trash"
                    title={t("delete")}
                    onClick={(e) => {
                      e.stopPropagation();
                      void handleDelete(entry);
                    }}
                    disabled={busy}
                    aria-label={`Delete ${entry.name}`}
                  >
                    <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" strokeWidth="2" fill="none" strokeLinecap="round" strokeLinejoin="round">
                      <polyline points="3 6 5 6 21 6" />
                      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
                      <line x1="10" y1="11" x2="10" y2="17" />
                      <line x1="14" y1="11" x2="14" y2="17" />
                    </svg>
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        {preview ? (
          <FilePreviewCard preview={preview} onClose={() => setPreview(null)} />
        ) : null}

        {share ? (
          <div className="card">
            <h3>{currentLang === "en" ? "Shared Link" : "Link condiviso"}</h3>
            <p className="muted">
              {currentLang === "en" ? "Expires at " : "Scade il "}
              {share.expires_at}
            </p>
            <div className="actions">
              <a className="btn small" href={share.url} target="_blank" rel="noreferrer">
                {currentLang === "en" ? "Open link" : "Apri link"}
              </a>
              <button
                className="btn small ghost"
                onClick={() => {
                  void navigator.clipboard?.writeText(share.url);
                }}
              >
                {currentLang === "en" ? "Copy" : "Copia"}
              </button>
              <button className="btn small ghost" onClick={() => setShare(null)}>
                {t("close")}
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
