"""
core/files.py

Read-only access to files produced by runs (``workspace/``), with previews and
short-lived share links.

Security model:
* every path is relative to the workspace root and is resolved *after* symlink
  resolution; anything that escapes the root (``..``, absolute paths, symlinked
  directories) is rejected;
* downloads stream from disk (no full-file read in RAM);
* previews are size-capped and never execute file content;
* share links are stateless HMAC tokens with a TTL, so no extra table is needed.

Uploads live in :mod:`core.uploads` (chunked, resumable, same path jail); this
module only lets a client inspect, download and share what already exists.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = Path(os.getenv("ALFREDO_FILES_ROOT") or (PROJECT_ROOT / "workspace")).resolve()
SECRET_FILE = PROJECT_ROOT / "config" / "share_secret.txt"
SHARE_SECRET_ENV = "ALFREDO_SHARE_SECRET"
MAX_PREVIEW_BYTES = 15 * 1024 * 1024
MAX_SHARE_TTL = 7 * 24 * 3600
DEFAULT_SHARE_TTL = 3600
MAX_LIST_ENTRIES = 1000

_DRIVE_RE = re.compile(r"^[A-Za-z]:")

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
TABLE_EXT = {".csv", ".tsv"}
SHEET_EXT = {".xlsx", ".xlsm"}
DOC_EXT = {".docx"}
PDF_EXT = {".pdf"}
TEXT_EXT = {
    ".txt", ".md", ".json", ".log", ".py", ".yml", ".yaml", ".toml", ".ini",
    ".html", ".htm", ".xml", ".sql", ".sh", ".ps1", ".bat", ".cfg", ".rst",
}


class FileAccessError(ValueError):
    """The requested path is invalid or outside the workspace."""


class FileNotFound(FileNotFoundError):
    """The requested path does not exist inside the workspace."""


# ---------------------------------------------------------------------------
# Path resolution (the jail)
# ---------------------------------------------------------------------------


def _root(root: Optional[Path] = None) -> Path:
    return Path(root).resolve() if root is not None else WORKSPACE_ROOT


def clean_relative(rel_path: str) -> str:
    """Normalize a client-provided relative path, rejecting traversal attempts."""
    raw = str(rel_path or "").replace("\\", "/").strip()
    if "\x00" in raw:
        raise FileAccessError("Invalid path")
    if raw.startswith("/") or _DRIVE_RE.match(raw):
        raise FileAccessError("Absolute paths are not allowed")
    parts: List[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FileAccessError("Path traversal is not allowed")
        if part.startswith("."):
            raise FileAccessError("Hidden paths are not exposed")
        parts.append(part)
    return "/".join(parts)


def resolve(rel_path: str, *, root: Optional[Path] = None, must_exist: bool = True) -> Path:
    """Resolve ``rel_path`` inside the workspace, following symlinks safely."""
    base = _root(root)
    clean = clean_relative(rel_path)
    candidate = (base / clean).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise FileAccessError("Path escapes the workspace") from exc

    if not candidate.exists():
        if must_exist:
            raise FileNotFound("File not found")
        return candidate
    # Resolve again strictly: a symlink inside the folder could point outside.
    real = candidate.resolve(strict=True)
    try:
        real.relative_to(base)
    except ValueError as exc:
        raise FileAccessError("Symlink escapes the workspace") from exc
    return real


def relative_to_workspace(path: Path, root: Optional[Path] = None) -> Optional[str]:
    base = _root(root)
    try:
        return path.resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Metadata / listing
# ---------------------------------------------------------------------------


def kind_of(name: str) -> str:
    ext = Path(str(name)).suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in PDF_EXT:
        return "pdf"
    if ext in SHEET_EXT or ext in TABLE_EXT:
        return "table"
    if ext in DOC_EXT:
        return "doc"
    if ext in TEXT_EXT:
        return "text"
    if ext in {".zip", ".tar", ".gz", ".7z"}:
        return "archive"
    return "binary"


def describe(rel_path: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Metadata for one entry. Raises FileAccessError/FileNotFound."""
    path = resolve(rel_path, root=root)
    rel = relative_to_workspace(path, root=root) or clean_relative(rel_path)
    stat = path.stat()
    return {
        "name": path.name,
        "path": rel,
        "is_dir": path.is_dir(),
        "size": int(stat.st_size),
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        "kind": "folder" if path.is_dir() else kind_of(path.name),
        "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def list_files(
    rel_path: str = "",
    *,
    root: Optional[Path] = None,
    limit: int = MAX_LIST_ENTRIES,
) -> Dict[str, Any]:
    """List a workspace folder (dirs first, then files, both alphabetical)."""
    folder = resolve(rel_path, root=root)
    if not folder.is_dir():
        raise FileAccessError("Not a directory")
    entries: List[Dict[str, Any]] = []
    for child in folder.iterdir():
        if child.name.startswith("."):
            continue
        try:
            entries.append(describe(relative_to_workspace(child, root=root) or child.name, root=root))
        except (FileAccessError, FileNotFound, OSError):
            continue
    entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
    truncated = len(entries) > max(1, int(limit))
    return {
        "path": clean_relative(rel_path),
        "entries": entries[: max(1, int(limit))],
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


def _table_rows(rows: List[List[Any]], max_rows: int, max_cols: int) -> Dict[str, Any]:
    truncated = len(rows) > max_rows
    trimmed = rows[:max_rows]
    columns_truncated = any(len(row) > max_cols for row in trimmed)
    return {
        "rows": [[("" if cell is None else str(cell)) for cell in row[:max_cols]] for row in trimmed],
        "truncated": truncated or columns_truncated,
    }


def _preview_xlsx(path: Path, max_rows: int, max_cols: int) -> Dict[str, Any]:
    from openpyxl import load_workbook

    workbook = load_workbook(filename=str(path), read_only=True, data_only=True)
    try:
        sheet_names = list(workbook.sheetnames)
        sheet = workbook[sheet_names[0]]
        rows: List[List[Any]] = []
        for index, row in enumerate(sheet.iter_rows(values_only=True)):
            if index >= max_rows + 1:
                break
            rows.append(list(row))
    finally:
        workbook.close()
    table = _table_rows(rows, max_rows, max_cols)
    return {"kind": "table", "sheets": sheet_names, "rows": table["rows"], "truncated": table["truncated"]}


def _preview_csv(path: Path, max_rows: int, max_cols: int) -> Dict[str, Any]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: List[List[str]] = []
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for index, row in enumerate(reader):
            if index > max_rows:
                break
            rows.append(row)
    table = _table_rows(rows, max_rows, max_cols)
    return {"kind": "table", "rows": table["rows"], "truncated": table["truncated"]}


def _preview_image(path: Path, max_side: int = 1200) -> Dict[str, Any]:
    from PIL import Image

    with Image.open(path) as image:
        image.thumbnail((max_side, max_side))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"kind": "image", "data_uri": f"data:image/jpeg;base64,{encoded}"}


def _preview_pdf(path: Path, max_chars: int) -> Dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = len(reader.pages)
    text = ""
    if pages:
        try:
            text = reader.pages[0].extract_text() or ""
        except Exception:  # noqa: BLE001 - some PDFs have no extractable text
            text = ""
    truncated = len(text) > max_chars
    return {
        "kind": "pdf",
        "pages": pages,
        "text": text[:max_chars],
        "truncated": truncated or pages > 1,
    }


def _preview_docx(path: Path, max_chars: int) -> Dict[str, Any]:
    from docx import Document

    document = Document(str(path))
    chunks: List[str] = []
    total = 0
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        chunks.append(text)
        total += len(text) + 1
        if total >= max_chars:
            break
    body = "\n".join(chunks)
    return {"kind": "text", "text": body[:max_chars], "truncated": len(body) > max_chars or total >= max_chars}


def preview(
    rel_path: str,
    *,
    root: Optional[Path] = None,
    max_chars: int = 8000,
    max_rows: int = 30,
    max_cols: int = 12,
    max_bytes: int = MAX_PREVIEW_BYTES,
) -> Dict[str, Any]:
    """Server-side preview: text, table, image thumbnail or PDF text."""
    path = resolve(rel_path, root=root)
    if path.is_dir():
        raise FileAccessError("Cannot preview a directory")
    rel = relative_to_workspace(path, root=root) or ""
    info = describe(rel, root=root)
    size = info["size"]
    if size > max_bytes:
        return {**info, "kind": "too_large", "message": "File troppo grande per l'anteprima."}

    ext = path.suffix.lower()
    try:
        if ext in IMAGE_EXT:
            return {**info, **_preview_image(path)}
        if ext in PDF_EXT:
            return {**info, **_preview_pdf(path, max_chars)}
        if ext in SHEET_EXT:
            return {**info, **_preview_xlsx(path, max_rows, max_cols)}
        if ext in TABLE_EXT:
            return {**info, **_preview_csv(path, max_rows, max_cols)}
        if ext in DOC_EXT:
            return {**info, **_preview_docx(path, max_chars)}
        if ext in TEXT_EXT or not ext:
            text = path.read_text(encoding="utf-8", errors="replace")
            return {**info, "kind": "text", "text": text[:max_chars], "truncated": len(text) > max_chars}
    except (FileAccessError, FileNotFound):
        raise
    except Exception as exc:  # noqa: BLE001 - a broken file must not 500 the API
        logger.warning("Preview failed for %s: %s", rel, exc, exc_info=True)
        return {**info, "kind": "binary", "error": f"Anteprima non disponibile: {exc}"}

    return {**info, "kind": "binary", "message": "Anteprima non supportata: scarica il file."}


# ---------------------------------------------------------------------------
# Run exports
# ---------------------------------------------------------------------------


def describe_run_exports(
    payload: Dict[str, Any], *, root: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """Map the ``exports`` paths stored on a run payload to workspace entries."""
    base = _root(root)
    entries: List[Dict[str, Any]] = []
    for raw in (payload or {}).get("exports") or []:
        text = str(raw or "").strip()
        if not text:
            continue
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = base / candidate
        rel = relative_to_workspace(candidate, root=base)
        if rel is None:
            # Outside the jail: expose the name only, never the server path.
            entries.append(
                {
                    "name": Path(text).name,
                    "path": "",
                    "accessible": False,
                    "exists": False,
                    "kind": kind_of(text),
                    "size": None,
                }
            )
            continue
        try:
            entry = describe(rel, root=base)
            entry.update({"accessible": True, "exists": True})
        except (FileNotFound, FileAccessError, OSError):
            entry = {
                "name": Path(text).name,
                "path": rel,
                "accessible": True,
                "exists": False,
                "kind": kind_of(text),
                "size": None,
            }
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------
# Share links (stateless HMAC + TTL)
# ---------------------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii"))


def share_secret() -> bytes:
    """Secret used to sign share links (env > config file > generated once)."""
    env_secret = (os.getenv(SHARE_SECRET_ENV) or "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    try:
        existing = SECRET_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing.encode("utf-8")
    except OSError:
        pass
    secret = _b64encode(os.urandom(32))
    try:
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(secret, encoding="utf-8")
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:  # pragma: no cover - Windows / read-only volumes
            pass
    except OSError as exc:  # pragma: no cover
        logger.warning("Could not persist share secret: %s", exc)
    return secret.encode("utf-8")


def create_share_token(
    rel_path: str, *, ttl_seconds: int = DEFAULT_SHARE_TTL, root: Optional[Path] = None
) -> Dict[str, Any]:
    """Create a signed, expiring link payload for one existing file."""
    path = resolve(rel_path, root=root)
    if path.is_dir():
        raise FileAccessError("Cannot share a directory")
    rel = relative_to_workspace(path, root=root) or clean_relative(rel_path)
    ttl = max(60, min(MAX_SHARE_TTL, int(ttl_seconds or DEFAULT_SHARE_TTL)))
    expires_at = int(time.time()) + ttl
    payload = _b64encode(json.dumps({"p": rel, "e": expires_at}, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(share_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    return {
        "token": f"{payload}.{signature}",
        "path": rel,
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires_at)),
        "ttl_seconds": ttl,
    }


def verify_share_token(token: str, *, root: Optional[Path] = None) -> Dict[str, Any]:
    """Validate a share token and return ``{"path", "expires_at"}`` or raise."""
    raw = str(token or "").strip()
    if "." not in raw:
        raise FileAccessError("Invalid share token")
    payload, signature = raw.rsplit(".", 1)
    expected = _b64encode(hmac.new(share_secret(), payload.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        raise FileAccessError("Invalid share token")
    try:
        data = json.loads(_b64decode(payload).decode("utf-8"))
        rel = str(data.get("p") or "")
        expires_at = int(data.get("e") or 0)
    except Exception as exc:  # noqa: BLE001
        raise FileAccessError("Invalid share token") from exc
    if expires_at < int(time.time()):
        raise FileAccessError("Share link expired")
    path = resolve(rel, root=root)
    return {"path": relative_to_workspace(path, root=root) or rel, "expires_at": expires_at}


__all__ = [
    "DEFAULT_SHARE_TTL",
    "FileAccessError",
    "FileNotFound",
    "MAX_SHARE_TTL",
    "WORKSPACE_ROOT",
    "clean_relative",
    "create_share_token",
    "describe",
    "describe_run_exports",
    "kind_of",
    "list_files",
    "preview",
    "relative_to_workspace",
    "resolve",
    "share_secret",
    "verify_share_token",
]
