"""
core/uploads.py

Chunked uploads into the workspace jail.

Design goals:
* never hold a whole file in RAM: the server receives one chunk per request and
  concatenates the parts on disk with a fixed 1 MB copy buffer;
* reuse the same path jail as :mod:`core.files` (hidden paths, ``..``, absolute
  paths, symlinked parents that escape ``workspace/`` are all rejected);
* sessions are resumable after a server restart because every piece of state
  lives in ``workspace/.uploads/<id>/`` (a hidden folder, so the jail can never
  expose it through the API);
* an optional SHA-256 of the whole file is verified before the destination is
  replaced, so a truncated/corrupted upload can never overwrite a real file.

Environment:
* ``ALFREDO_MAX_UPLOAD_MB``     total file size cap (default 512 MB)
* ``ALFREDO_UPLOAD_TTL_MINUTES`` stale session TTL (default 24 h)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core import files

logger = logging.getLogger(__name__)

UPLOAD_DIR_NAME = ".uploads"
DEFAULT_CHUNK_BYTES = 1024 * 1024
MIN_CHUNK_BYTES = 64 * 1024
MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 4096
MAX_SESSIONS = 64
MAX_UPLOAD_BYTES = int(float(os.getenv("ALFREDO_MAX_UPLOAD_MB", "512")) * 1024 * 1024)
SESSION_TTL_SECONDS = int(float(os.getenv("ALFREDO_UPLOAD_TTL_MINUTES", "1440")) * 60)

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_ASSEMBLY_LOCK = threading.Lock()
_COPY_BUFFER = 1024 * 1024


class UploadError(ValueError):
    """The upload request is invalid (bad path, size, chunk, checksum)."""


class UploadNotFound(FileNotFoundError):
    """Unknown or already-finished upload session."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base(root: Optional[Path] = None) -> Path:
    return Path(root).resolve() if root is not None else files.WORKSPACE_ROOT


def _uploads_dir(root: Optional[Path] = None) -> Path:
    return _base(root) / UPLOAD_DIR_NAME


def _check_id(upload_id: str) -> str:
    raw = str(upload_id or "").strip().lower()
    if not _ID_RE.match(raw):
        raise UploadNotFound("Upload not found")
    return raw


def _session_dir(upload_id: str, root: Optional[Path] = None) -> Path:
    return _uploads_dir(root) / _check_id(upload_id)


def _meta_path(upload_id: str, root: Optional[Path] = None) -> Path:
    return _session_dir(upload_id, root) / "meta.json"


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _load_meta(upload_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
    path = _meta_path(upload_id, root)
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UploadNotFound("Upload not found") from exc
    if not isinstance(meta, dict) or meta.get("id") != _check_id(upload_id):
        raise UploadNotFound("Upload not found")
    if float(meta.get("expires_at") or 0) < time.time():
        raise UploadError("Upload session expired")
    return meta


def _received(session: Path) -> List[int]:
    indexes: List[int] = []
    try:
        children = list(session.iterdir())
    except OSError:
        return indexes
    for child in children:
        match = re.match(r"^(\d+)\.part$", child.name)
        if match and child.is_file():
            indexes.append(int(match.group(1)))
    return sorted(indexes)


def _session_payload(meta: Dict[str, Any], session: Path) -> Dict[str, Any]:
    received = _received(session)
    return {
        "upload_id": meta["id"],
        "path": meta["path"],
        "size": int(meta["size"]),
        "chunk_size": int(meta["chunk_size"]),
        "chunks": int(meta["chunks"]),
        "received": received,
        "complete": len(received) >= int(meta["chunks"]),
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(meta["expires_at"]))),
    }


def _normalize_sha(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if not _HEX_RE.match(text):
        raise UploadError("sha256 must be 64 hexadecimal characters")
    return text


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def cleanup_stale(now: Optional[float] = None, *, root: Optional[Path] = None) -> int:
    """Remove expired sessions (and the leftovers of interrupted assemblies)."""
    base = _base(root)
    folder = _uploads_dir(base)
    if not folder.is_dir():
        return 0
    current = time.time() if now is None else float(now)
    removed = 0
    for child in folder.iterdir():
        if not child.is_dir():
            continue
        expires = 0.0
        try:
            meta = json.loads((child / "meta.json").read_text(encoding="utf-8"))
            expires = float(meta.get("expires_at") or 0)
        except (OSError, ValueError, TypeError):
            expires = 0.0
        if not expires:
            # No meta: only clean when the folder itself is old enough.
            try:
                expires = child.stat().st_mtime + SESSION_TTL_SECONDS
            except OSError:
                expires = current + 1
        if expires < current:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def _trim_sessions(root: Optional[Path]) -> None:
    folder = _uploads_dir(root)
    sessions: List[tuple[float, Path]] = []
    for child in folder.iterdir():
        if not child.is_dir():
            continue
        try:
            created = float(
                json.loads((child / "meta.json").read_text(encoding="utf-8")).get("created_at") or 0
            )
        except (OSError, ValueError, TypeError):
            created = 0.0
        sessions.append((created, child))
    if len(sessions) < MAX_SESSIONS:
        return
    sessions.sort(key=lambda item: item[0])
    for _, child in sessions[: len(sessions) - MAX_SESSIONS]:
        shutil.rmtree(child, ignore_errors=True)


def start_upload(
    path: str,
    size: int,
    *,
    chunk_size: int = DEFAULT_CHUNK_BYTES,
    sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Create an upload session for ``path`` (relative to the workspace)."""
    rel = files.clean_relative(path)
    if not rel:
        raise UploadError("A destination file path is required")
    try:
        total = int(size)
    except (TypeError, ValueError) as exc:
        raise UploadError("size must be an integer number of bytes") from exc
    if total <= 0:
        raise UploadError("Cannot upload an empty file")
    if total > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"File too large: {total} bytes (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )
    try:
        step = int(chunk_size or DEFAULT_CHUNK_BYTES)
    except (TypeError, ValueError) as exc:
        raise UploadError("chunk_size must be an integer") from exc
    if step < MIN_CHUNK_BYTES or step > MAX_CHUNK_BYTES:
        raise UploadError(
            f"chunk_size must be between {MIN_CHUNK_BYTES} and {MAX_CHUNK_BYTES} bytes"
        )
    chunks = (total + step - 1) // step
    if chunks > MAX_CHUNKS:
        raise UploadError(f"Too many chunks ({chunks}); use a bigger chunk_size")
    digest = _normalize_sha(sha256)

    base = _base(root)
    destination = base / rel
    if destination.exists() and destination.is_dir():
        raise UploadError("Destination is a directory")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.resolve().relative_to(base)
    except (OSError, ValueError) as exc:
        raise files.FileAccessError("Path escapes the workspace") from exc

    folder = _uploads_dir(base)
    folder.mkdir(parents=True, exist_ok=True)
    cleanup_stale(root=base)

    upload_id = os.urandom(16).hex()
    session = folder / upload_id
    session.mkdir(parents=True, exist_ok=True)
    now = time.time()
    meta = {
        "id": upload_id,
        "path": rel,
        "size": total,
        "chunk_size": step,
        "chunks": chunks,
        "sha256": digest,
        "created_at": now,
        "expires_at": now + SESSION_TTL_SECONDS,
    }
    try:
        _write_json_atomic(session / "meta.json", meta)
        _trim_sessions(base)
    except OSError as exc:
        shutil.rmtree(session, ignore_errors=True)
        raise UploadError(f"Cannot create upload session: {exc}") from exc
    logger.info("Upload %s started for %s (%s bytes, %s chunks)", upload_id, rel, total, chunks)
    return _session_payload(meta, session)


def upload_status(upload_id: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Current progress of a session (used to resume after a reconnect)."""
    meta = _load_meta(upload_id, root)
    return _session_payload(meta, _session_dir(upload_id, root))


def write_chunk(
    upload_id: str,
    index: int,
    data: bytes,
    *,
    chunk_sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist one chunk. Re-sending the same index is idempotent."""
    meta = _load_meta(upload_id, root)
    try:
        position = int(index)
    except (TypeError, ValueError) as exc:
        raise UploadError("Chunk index must be an integer") from exc
    chunks = int(meta["chunks"])
    if position < 0 or position >= chunks:
        raise UploadError(f"Chunk index out of range (0..{chunks - 1})")
    payload = bytes(data or b"")
    step = int(meta["chunk_size"])
    total = int(meta["size"])
    expected = step if position < chunks - 1 else total - step * (chunks - 1)
    if len(payload) != expected:
        raise UploadError(
            f"Chunk {position} must be {expected} bytes (got {len(payload)})"
        )
    if len(payload) > MAX_CHUNK_BYTES:
        raise UploadError("Chunk too large")
    wanted = _normalize_sha(chunk_sha256)
    if wanted and hashlib.sha256(payload).hexdigest() != wanted:
        raise UploadError(f"Checksum mismatch for chunk {position}")

    session = _session_dir(upload_id, root)
    target = session / f"{position}.part"
    tmp = session / f"{position}.part.tmp"
    try:
        tmp.write_bytes(payload)
        os.replace(str(tmp), str(target))
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise UploadError(f"Cannot store chunk {position}: {exc}") from exc
    received = _received(session)
    return {
        "upload_id": meta["id"],
        "index": position,
        "received": received,
        "chunks": chunks,
        "complete": len(received) >= chunks,
    }


def _assemble(
    session: Path,
    chunks: int,
    target: Path,
    expected_size: int,
) -> tuple[int, str]:
    """Concatenate parts into ``target`` with a fixed buffer, hashing as we go."""
    digest = hashlib.sha256()
    written = 0
    with target.open("wb") as output:
        for position in range(chunks):
            part = session / f"{position}.part"
            with part.open("rb") as handle:
                while True:
                    block = handle.read(_COPY_BUFFER)
                    if not block:
                        break
                    written += len(block)
                    if written > expected_size:
                        raise UploadError("Upload is larger than declared")
                    digest.update(block)
                    output.write(block)
    return written, digest.hexdigest()


def complete_upload(
    upload_id: str,
    *,
    sha256: Optional[str] = None,
    root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Verify and move the assembled file into its workspace destination."""
    base = _base(root)
    meta = _load_meta(upload_id, root)
    session = _session_dir(upload_id, root)
    chunks = int(meta["chunks"])
    missing = [index for index in range(chunks) if not (session / f"{index}.part").is_file()]
    if missing:
        preview = ", ".join(str(index) for index in missing[:8])
        raise UploadError(f"Missing chunks: {preview}" + ("…" if len(missing) > 8 else ""))

    rel = files.clean_relative(str(meta["path"]))
    destination = base / rel
    if destination.exists() and destination.is_dir():
        raise UploadError("Destination is a directory")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.parent.resolve().relative_to(base)
    except (OSError, ValueError) as exc:
        raise files.FileAccessError("Path escapes the workspace") from exc

    expected_sha = _normalize_sha(sha256) or _normalize_sha(meta.get("sha256"))
    assembled = session / "assembled.bin"
    with _ASSEMBLY_LOCK:
        try:
            written, digest = _assemble(session, chunks, assembled, int(meta["size"]))
        except UploadError:
            assembled.unlink(missing_ok=True)
            raise
        if written != int(meta["size"]):
            assembled.unlink(missing_ok=True)
            raise UploadError(
                f"Size mismatch: expected {meta['size']} bytes, assembled {written}"
            )
        if expected_sha and digest != expected_sha:
            assembled.unlink(missing_ok=True)
            raise UploadError("Checksum mismatch")
        try:
            os.replace(str(assembled), str(destination))
        except OSError as exc:
            raise UploadError(f"Cannot write {rel}: {exc}") from exc

    shutil.rmtree(session, ignore_errors=True)
    logger.info("Upload %s completed -> %s (%s bytes)", upload_id, rel, written)
    entry = files.describe(rel, root=base)
    entry.update({"upload_id": meta["id"], "sha256": digest})
    return entry


def abort_upload(upload_id: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Delete a session and all its parts."""
    session = _session_dir(upload_id, root)
    if not session.is_dir():
        raise UploadNotFound("Upload not found")
    shutil.rmtree(session, ignore_errors=True)
    return {"upload_id": _check_id(upload_id), "aborted": True}


__all__ = [
    "DEFAULT_CHUNK_BYTES",
    "MAX_CHUNK_BYTES",
    "MAX_UPLOAD_BYTES",
    "MIN_CHUNK_BYTES",
    "SESSION_TTL_SECONDS",
    "UploadError",
    "UploadNotFound",
    "abort_upload",
    "cleanup_stale",
    "complete_upload",
    "start_upload",
    "upload_status",
    "write_chunk",
]
