import { api } from "./api";
import type { ConnectionConfig, RunFile } from "./types";

export const UPLOAD_CHUNK_SIZE = 1024 * 1024;
const HASH_LIMIT = 8 * 1024 * 1024; // WebCrypto needs the whole buffer: hash only small files.

export interface UploadProgress {
  name: string;
  sent: number;
  total: number;
}

async function sha256Hex(blob: Blob): Promise<string | undefined> {
  if (!globalThis.crypto?.subtle) return undefined;
  const buffer = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function joinPath(folder: string, name: string): string {
  return folder ? `${folder}/${name}` : name;
}

/**
 * Upload one browser File in chunks.
 *
 * Only one chunk is ever in memory/on the wire, so a 200 MB file behaves like a
 * small one. Small files also carry a SHA-256 the server verifies before moving
 * the result into place.
 */
export async function uploadBrowserFile(
  config: ConnectionConfig,
  file: File,
  folder: string,
  onProgress?: (progress: UploadProgress) => void,
): Promise<RunFile> {
  if (file.size <= 0) throw new Error("Il file è vuoto.");
  const target = joinPath(folder.replace(/^\/+|\/+$/g, ""), file.name);
  const session = await api.startUpload(config, target, file.size, UPLOAD_CHUNK_SIZE);
  const checksum = file.size <= HASH_LIMIT ? await sha256Hex(file) : undefined;
  try {
    for (let index = 0; index < session.chunks; index++) {
      if (session.received?.includes(index)) continue;
      const start = index * session.chunk_size;
      const chunk = file.slice(start, Math.min(start + session.chunk_size, file.size));
      await api.uploadChunk(config, session.upload_id, index, chunk);
      onProgress?.({ name: file.name, sent: start + chunk.size, total: file.size });
    }
    return await api.completeUpload(config, session.upload_id, checksum);
  } catch (error) {
    void api.abortUpload(config, session.upload_id).catch(() => undefined);
    throw error;
  }
}
