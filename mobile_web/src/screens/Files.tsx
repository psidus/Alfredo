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
import type { Language } from "../i18n";

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

export function FilesScreen({ config, lang = "it" }: Props) {
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
    async (folder: string) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setLoading(true);
      try {
        const listing = await api.listFiles(config, folder);
        setEntries(listing.entries ?? []);
        setError(null);
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
    setPreview(null);
    setShare(null);
    void load(path);
  }, [load, path]);

  // Cheap refresh so files produced by a running workflow show up.
  usePolling(() => void load(path), 15000, true);

  const uploadFiles = async (files: File[]) => {
    if (!files.length) return;
    setBusy(true);
    setError(null);
    try {
      for (const file of files) {
        setUpload({ name: file.name, sent: 0, total: file.size });
        await uploadBrowserFile(config, file, path, (progress) => setUpload(progress));
      }
      await load(path);
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setUpload(null);
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const openPreview = async (entry: RunFile) => {
    if (entry.is_dir) {
      setPath(entry.path);
      return;
    }
    setBusy(true);
    setError(null);
    setShare(null);
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
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = entry.name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 2000);
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
      setShare(await api.shareFile(config, entry.path, 3600));
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
          className="btn small ghost"
          onClick={() => setPath(parentOf(path))}
          disabled={!path}
          title={lang === "en" ? "Parent folder" : "Cartella superiore"}
        >
          <span className="arrow-left-icon">←</span>
          <span>{lang === "en" ? "Up" : "Su"}</span>
        </button>
        <button
          className="btn small ghost"
          onClick={() => void load(path)}
          disabled={loading}
          aria-label="Aggiorna"
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
              <p className="muted">File prodotti dai workflow e caricati da qui.</p>
            </div>
            <button
              className="btn small primary"
              onClick={() => inputRef.current?.click()}
              disabled={busy}
            >
              Carica qui
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
              {loading ? "carico…" : `${entries.length} elementi`}
            </span>
          </div>
          {loading && entries.length === 0 ? (
            <div className="empty row" style={{ justifyContent: "center" }}>
              <Spinner /> Carico…
            </div>
          ) : entries.length === 0 ? (
            <p className="muted">Cartella vuota.</p>
          ) : (
            entries.map((entry) => (
              <div className="file-row" key={entry.path || entry.name}>
                <div className="file-meta">
                  <strong>
                    {iconFor(entry)} {entry.name}
                  </strong>
                  <span className="muted">
                    {entry.is_dir ? "cartella" : entry.kind}
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
                    {entry.is_dir ? "Apri" : "Anteprima"}
                  </button>
                  {!entry.is_dir ? (
                    <>
                      <button
                        className="btn small"
                        onClick={() => void download(entry)}
                        disabled={busy}
                      >
                        Scarica
                      </button>
                      <button
                        className="btn small ghost"
                        onClick={() => void createShare(entry)}
                        disabled={busy}
                      >
                        Link
                      </button>
                    </>
                  ) : null}
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
            <h3>Link condiviso</h3>
            <p className="muted">Scade il {share.expires_at}</p>
            <div className="actions">
              <a className="btn small" href={share.url} target="_blank" rel="noreferrer">
                Apri link
              </a>
              <button
                className="btn small ghost"
                onClick={() => {
                  void navigator.clipboard?.writeText(share.url);
                }}
              >
                Copia
              </button>
              <button className="btn small ghost" onClick={() => setShare(null)}>
                Chiudi
              </button>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
