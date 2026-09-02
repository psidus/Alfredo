"""
Alfredo Hub registry database (separate from runtime alfredo_db).

Uses HUB_DATABASE_URL (Postgres). Falls back to a local SQLite file
db/hub.sqlite when HUB_DATABASE_URL is unset (dev / offline hub).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class HubDB:
    """Thin registry store for shared workflow packages."""

    def __init__(self, db_url: Optional[str] = None):
        self.db_url = db_url or os.environ.get("HUB_DATABASE_URL") or ""
        self._pg = None
        self._sqlite = None
        if self.db_url.startswith("postgres"):
            import psycopg2
            from psycopg2.extras import RealDictCursor
            self._pg = psycopg2.connect(self.db_url)
            self._pg.autocommit = True
            self._cursor_factory = RealDictCursor
            self._use_pg = True
        else:
            path = self.db_url.replace("sqlite:///", "") if self.db_url.startswith("sqlite") else os.path.join("db", "hub.sqlite")
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._sqlite = sqlite3.connect(path, check_same_thread=False)
            self._sqlite.row_factory = sqlite3.Row
            self._use_pg = False
        self._ensure_schema()

    def close(self):
        if self._pg:
            self._pg.close()
        if self._sqlite:
            self._sqlite.close()

    def _execute(self, sql: str, params: tuple = (), fetch: str = "none"):
        if self._use_pg:
            sql_pg = sql.replace("?", "%s")
            with self._pg.cursor(cursor_factory=self._cursor_factory) as cur:
                cur.execute(sql_pg, params)
                if fetch == "one":
                    row = cur.fetchone()
                    return dict(row) if row else None
                if fetch == "all":
                    return [dict(r) for r in cur.fetchall()]
                if fetch == "id":
                    row = cur.fetchone()
                    return row["id"] if row else None
                return None
        else:
            cur = self._sqlite.cursor()
            cur.execute(sql, params)
            self._sqlite.commit()
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row else None
            if fetch == "all":
                return [dict(r) for r in cur.fetchall()]
            if fetch == "id":
                return cur.lastrowid
            return None

    def _ensure_schema(self):
        if self._use_pg:
            stmts = [
                """
                CREATE TABLE IF NOT EXISTS hub_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    org_slug TEXT DEFAULT '',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS hub_packages (
                    id SERIAL PRIMARY KEY,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    author_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE,
                    org_slug TEXT DEFAULT '',
                    format_version TEXT DEFAULT '1.0',
                    package_version TEXT DEFAULT '1.0.0',
                    package_json JSONB NOT NULL,
                    checksum TEXT DEFAULT '',
                    downloads INTEGER DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(author_id, slug, package_version)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS hub_shares (
                    id SERIAL PRIMARY KEY,
                    package_id INTEGER REFERENCES hub_packages(id) ON DELETE CASCADE,
                    shared_with_user_id INTEGER REFERENCES hub_users(id) ON DELETE CASCADE,
                    UNIQUE(package_id, shared_with_user_id)
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_hub_packages_visibility ON hub_packages(visibility)",
                "CREATE INDEX IF NOT EXISTS idx_hub_packages_org ON hub_packages(org_slug)",
            ]
            for s in stmts:
                self._execute(s)
            # full-text helper index (title + description)
            try:
                self._execute(
                    "CREATE INDEX IF NOT EXISTS idx_hub_packages_fts ON hub_packages "
                    "USING GIN (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(description,'')))"
                )
            except Exception:
                pass
        else:
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS hub_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    org_slug TEXT DEFAULT '',
                    created_at TEXT
                )
                """
            )
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS hub_packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    author_id INTEGER,
                    org_slug TEXT DEFAULT '',
                    format_version TEXT DEFAULT '1.0',
                    package_version TEXT DEFAULT '1.0.0',
                    package_json TEXT NOT NULL,
                    checksum TEXT DEFAULT '',
                    downloads INTEGER DEFAULT 0,
                    created_at TEXT,
                    updated_at TEXT,
                    UNIQUE(author_id, slug, package_version)
                )
                """
            )
            self._execute(
                """
                CREATE TABLE IF NOT EXISTS hub_shares (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER,
                    shared_with_user_id INTEGER,
                    UNIQUE(package_id, shared_with_user_id)
                )
                """
            )

    # --- users ---
    def register_user(self, username: str, display_name: str = "", org_slug: str = "") -> Tuple[Dict[str, Any], str]:
        username = (username or "").strip().lower()
        if not username:
            raise ValueError("username required")
        existing = self._execute("SELECT * FROM hub_users WHERE username = ?", (username,), fetch="one")
        if existing:
            raise ValueError("username already taken")
        org_slug = (org_slug or "").strip().lower()
        token = secrets.token_urlsafe(32)
        if self._use_pg:
            row = self._execute(
                "INSERT INTO hub_users (username, token_hash, display_name, org_slug) "
                "VALUES (?, ?, ?, ?) RETURNING id, username, display_name, org_slug",
                (username, _hash_token(token), display_name or username, org_slug or ""),
                fetch="one",
            )
        else:
            uid = self._execute(
                "INSERT INTO hub_users (username, token_hash, display_name, org_slug, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, _hash_token(token), display_name or username, org_slug or "", _utcnow()),
                fetch="id",
            )
            row = {"id": uid, "username": username, "display_name": display_name or username, "org_slug": org_slug or ""}
        return row, token

    def authenticate(self, username: str, token: str) -> Optional[Dict[str, Any]]:
        username = (username or "").strip().lower()
        row = self._execute("SELECT * FROM hub_users WHERE username = ?", (username,), fetch="one")
        if not row:
            return None
        if row["token_hash"] != _hash_token(token):
            return None
        return dict(row)

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        return self._execute(
            "SELECT id, username, display_name, org_slug FROM hub_users WHERE username = ?",
            ((username or "").strip().lower(),),
            fetch="one",
        )

    def rotate_token(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        self._execute("UPDATE hub_users SET token_hash = ? WHERE id = ?", (_hash_token(token), user_id))
        return token

    # --- packages ---
    def publish_package(
        self,
        author: Dict[str, Any],
        *,
        slug: str,
        title: str,
        description: str,
        tags: List[str],
        visibility: str,
        package_json: Dict[str, Any],
        checksum: str,
        package_version: str = "1.0.0",
        format_version: str = "1.0",
        share_usernames: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        visibility = (visibility or "private").lower()
        if visibility not in ("private", "org", "public"):
            raise ValueError("visibility must be private|org|public")
        org_slug = (author.get("org_slug") or "").strip().lower()
        if visibility == "org" and not org_slug:
            raise ValueError("org visibility requires user org_slug")

        tags_json = json.dumps(tags or [])
        pkg_body = json.dumps(package_json, ensure_ascii=False, default=str)
        now = _utcnow()

        existing = self._execute(
            "SELECT id FROM hub_packages WHERE author_id = ? AND slug = ? AND package_version = ?",
            (author["id"], slug, package_version),
            fetch="one",
        )
        if existing:
            self._execute(
                "UPDATE hub_packages SET title = ?, description = ?, tags = ?, visibility = ?, "
                "org_slug = ?, package_json = ?, checksum = ?, updated_at = ? WHERE id = ?",
                (title, description or "", tags_json, visibility, org_slug, pkg_body, checksum, now, existing["id"]),
            )
            pkg_id = existing["id"]
        elif self._use_pg:
            row = self._execute(
                "INSERT INTO hub_packages "
                "(slug, title, description, tags, visibility, author_id, org_slug, format_version, "
                "package_version, package_json, checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CAST(? AS jsonb), ?) RETURNING id",
                (slug, title, description or "", tags_json, visibility, author["id"], org_slug,
                 format_version, package_version, pkg_body, checksum),
                fetch="one",
            )
            pkg_id = row["id"]
        else:
            pkg_id = self._execute(
                "INSERT INTO hub_packages "
                "(slug, title, description, tags, visibility, author_id, org_slug, format_version, "
                "package_version, package_json, checksum, downloads, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (slug, title, description or "", tags_json, visibility, author["id"], org_slug,
                 format_version, package_version, pkg_body, checksum, now, now),
                fetch="id",
            )

        # shares
        for uname in share_usernames or []:
            u = self.get_user_by_username(uname)
            if not u:
                continue
            try:
                self._execute(
                    "INSERT INTO hub_shares (package_id, shared_with_user_id) VALUES (?, ?)",
                    (pkg_id, u["id"]),
                )
            except Exception:
                pass

        return self.get_package(pkg_id, include_body=False)

    def _parse_package_row(self, row: Dict[str, Any], include_body: bool) -> Dict[str, Any]:
        if not row:
            return row
        out = dict(row)
        tags = out.get("tags") or "[]"
        if isinstance(tags, str):
            try:
                out["tags"] = json.loads(tags)
            except Exception:
                out["tags"] = []
        body = out.get("package_json")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except Exception:
                body = {}
        if include_body:
            out["package_json"] = body
        else:
            out.pop("package_json", None)
        return out

    def get_package(self, package_id: int, include_body: bool = True) -> Optional[Dict[str, Any]]:
        row = self._execute("SELECT * FROM hub_packages WHERE id = ?", (package_id,), fetch="one")
        return self._parse_package_row(row, include_body) if row else None

    def increment_downloads(self, package_id: int):
        self._execute("UPDATE hub_packages SET downloads = downloads + 1 WHERE id = ?", (package_id,))

    def share_package(self, package_id: int, owner_id: int, username: str) -> bool:
        pkg = self.get_package(package_id, include_body=False)
        if not pkg or pkg.get("author_id") != owner_id:
            raise ValueError("Package not found or not owned by you")
        u = self.get_user_by_username(username)
        if not u:
            raise ValueError(f"User '{username}' not found")
        try:
            self._execute(
                "INSERT INTO hub_shares (package_id, shared_with_user_id) VALUES (?, ?)",
                (package_id, u["id"]),
            )
        except Exception:
            pass
        return True

    def delete_package(self, package_id: int, owner_id: int) -> bool:
        pkg = self.get_package(package_id, include_body=False)
        if not pkg:
            raise ValueError("Package not found")
        if pkg.get("author_id") != owner_id:
            raise ValueError("Only the author can delete this package")
        self._execute("DELETE FROM hub_shares WHERE package_id = ?", (package_id,))
        self._execute(
            "DELETE FROM hub_packages WHERE id = ? AND author_id = ?",
            (package_id, owner_id),
        )
        return True

    def user_can_access(self, pkg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> bool:
        if pkg.get("visibility") == "public":
            return True
        if not user:
            return False
        if pkg.get("author_id") == user.get("id"):
            return True
        pkg_org = (pkg.get("org_slug") or "").strip().lower()
        user_org = (user.get("org_slug") or "").strip().lower()
        if pkg.get("visibility") == "org" and pkg_org and pkg_org == user_org:
            return True
        share = self._execute(
            "SELECT 1 AS ok FROM hub_shares WHERE package_id = ? AND shared_with_user_id = ?",
            (pkg["id"], user["id"]),
            fetch="one",
        )
        return bool(share)

    def search_packages(
        self,
        query: str = "",
        tag: str = "",
        user: Optional[Dict[str, Any]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        rows = self._execute(
            "SELECT p.*, u.username AS author_username FROM hub_packages p "
            "JOIN hub_users u ON u.id = p.author_id ORDER BY p.updated_at DESC LIMIT 500",
            (),
            fetch="all",
        ) or []
        q = (query or "").strip().lower()
        tag_l = (tag or "").strip().lower()
        results = []
        for row in rows:
            pkg = self._parse_package_row(row, include_body=False)
            if not self.user_can_access(pkg, user):
                continue
            if tag_l:
                tags = [str(t).lower() for t in (pkg.get("tags") or [])]
                if tag_l not in tags:
                    continue
            if q:
                blob = f"{pkg.get('title','')} {pkg.get('description','')} {pkg.get('author_username','')} {pkg.get('slug','')}".lower()
                tags_blob = " ".join(str(t) for t in (pkg.get("tags") or [])).lower()
                if q not in blob and q not in tags_blob:
                    continue
            results.append(pkg)
            if len(results) >= limit:
                break
        return results
