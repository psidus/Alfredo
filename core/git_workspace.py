"""
core/git_workspace.py

Git-Backed Workspace, Time-Travel, Smart Diff (HITL), Worktree Isolation & GitOps Engine
for Alfredo AI OS.

Uses GitPython under the hood so end users get zero-code:
1. Automatic step-by-step checkpointing of all files in `workspace/`
2. Semantic & Office-aware (.docx/.xlsx/.csv/.md) Smart Diffs for Human-in-the-Loop approvals
3. Instant 1-click Time-Travel (Rollback) of the entire workspace or individual files
4. Parallel DAG node isolation via `git worktree`
5. GitOps versioning of Agent prompts, Tasks, and Workflows (including LearningMemory edits)
"""

from __future__ import annotations

import difflib
import io
import json
import os
import re
import shutil
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    import git
    from git import Repo, InvalidGitRepositoryError, GitCommandError
    GITPYTHON_AVAILABLE = True
except ImportError:
    GITPYTHON_AVAILABLE = False
    Repo = Any  # type: ignore


_GLOBAL_GIT_LOCK = threading.RLock()

DEFAULT_GITIGNORE = """# Alfredo AI OS Workspace Ignore
.worktrees/
__pycache__/
*.py[cod]
*$py.class
*.tmp
*.bak
~$*
.DS_Store
Thumbs.db
"""


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (value or "item").strip()).strip("_").lower()
    return slug[:60] or "item"


class GitWorkspaceManager:
    """
    Thread-safe manager that turns `workspace/` into a versioned Time-Travel repository.
    """

    def __init__(self, workspace_dir: Optional[str] = None):
        if workspace_dir:
            self.workspace_dir = os.path.abspath(workspace_dir)
        else:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            self.workspace_dir = os.path.join(project_root, "workspace")

        self.gitops_dir = os.path.join(self.workspace_dir, ".alfredo_gitops")
        self.worktrees_dir = os.path.join(self.workspace_dir, ".worktrees")
        self.enabled = GITPYTHON_AVAILABLE
        self.repo: Optional[Repo] = None

        if self.enabled:
            self._ensure_repo()

    # ------------------------------------------------------------------
    # Repository Initialization
    # ------------------------------------------------------------------
    def _ensure_repo(self) -> Optional[Repo]:
        if not self.enabled:
            return None

        with _GLOBAL_GIT_LOCK:
            os.makedirs(self.workspace_dir, exist_ok=True)
            git_dir = os.path.join(self.workspace_dir, ".git")
            try:
                if os.path.exists(git_dir):
                    self.repo = Repo(self.workspace_dir)
                else:
                    self.repo = Repo.init(self.workspace_dir)
            except Exception as e:
                print(f"[GitWorkspace] Warning: could not init repo at {self.workspace_dir}: {e}")
                self.enabled = False
                return None

            try:
                with self.repo.config_writer() as cw:
                    if not cw.has_option("user", "name"):
                        cw.set_value("user", "name", "Alfredo AI OS")
                    if not cw.has_option("user", "email"):
                        cw.set_value("user", "email", "alfredo@local.ai")
                    cw.set_value("core", "autocrlf", "false")
                    cw.set_value("core", "quotepath", "false")
            except Exception:
                pass

            gitignore_path = os.path.join(self.workspace_dir, ".gitignore")
            if not os.path.exists(gitignore_path):
                try:
                    with open(gitignore_path, "w", encoding="utf-8") as f:
                        f.write(DEFAULT_GITIGNORE)
                except Exception:
                    pass

            # Create initial commit if repository has no commits yet
            if not self.repo.head.is_valid():
                try:
                    self.repo.git.add(A=True)
                    self.repo.index.commit("[Init] Alfredo Workspace Time-Travel initialized")
                except Exception as e:
                    print(f"[GitWorkspace] Initial commit warning: {e}")

            return self.repo

    def get_head_sha(self) -> Optional[str]:
        """Returns the current HEAD commit SHA of the workspace repository."""
        if not self.enabled or not self.repo:
            return None
        with _GLOBAL_GIT_LOCK:
            try:
                if self.repo.head.is_valid():
                    return self.repo.head.commit.hexsha
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Office / Binary Text Extraction for Semantic Diffs
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_readable_lines_from_bytes(file_bytes: bytes, ext: str) -> Optional[List[str]]:
        """
        Extracts human-readable text lines from binary Office files (.docx, .xlsx)
        or text files so we can compute clean unified diffs even on Word/Excel documents.
        """
        ext = ext.lower()
        if ext == ".docx":
            try:
                import docx
                doc = docx.Document(io.BytesIO(file_bytes))
                lines = []
                for p in doc.paragraphs:
                    if p.text.strip():
                        lines.append(p.text)
                for idx, table in enumerate(doc.tables):
                    lines.append(f"--- [Table {idx + 1}] ---")
                    for row in table.rows:
                        lines.append(" | ".join(cell.text.strip() for cell in row.cells))
                return lines
            except Exception:
                return None

        if ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
                lines = []
                for sheet in wb.sheetnames:
                    ws = wb[sheet]
                    lines.append(f"=== Sheet: {sheet} ===")
                    for row in list(ws.iter_rows(values_only=True))[:200]:
                        if any(cell is not None for cell in row):
                            lines.append(" | ".join("" if c is None else str(c) for c in row))
                return lines
            except Exception:
                return None

        # Standard UTF-8 / Latin-1 text fallback
        try:
            text = file_bytes.decode("utf-8")
            return text.splitlines()
        except UnicodeDecodeError:
            try:
                text = file_bytes.decode("latin-1")
                return text.splitlines()
            except Exception:
                return None

    def _read_blob_bytes(self, commit_sha: str, rel_path: str) -> Optional[bytes]:
        if not self.repo or not commit_sha:
            return None
        try:
            commit = self.repo.commit(commit_sha)
            blob = commit.tree / rel_path.replace("\\", "/")
            return blob.data_stream.read()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Smart Diff Engine (Used by HITL & Streamlit UI)
    # ------------------------------------------------------------------
    def get_smart_diff(
        self,
        from_sha: Optional[str] = None,
        to_sha: Optional[str] = None,
        max_chars: int = 3500,
        include_gitops: bool = False,
    ) -> Dict[str, Any]:
        """
        Computes a structured summary and human-readable unified diff between:
        - `from_sha` and `to_sha` (if both provided), OR
        - `from_sha` (or HEAD) and the current working tree (including untracked files).
        Supports .docx and .xlsx semantic text diffing!
        """
        result: Dict[str, Any] = {
            "has_changes": False,
            "changed_files": [],
            "summary": "Nessuna modifica ai file del workspace.",
            "diff_text": "",
        }
        if not self.enabled or not self.repo:
            return result

        with _GLOBAL_GIT_LOCK:
            try:
                base_sha = from_sha or self.get_head_sha()
                if not base_sha:
                    return result

                base_commit = self.repo.commit(base_sha)
                changed_files: List[Dict[str, str]] = []
                diff_blocks: List[str] = []

                if to_sha:
                    target_commit = self.repo.commit(to_sha)
                    diffs = base_commit.diff(target_commit)
                    for d in diffs:
                        rel_path = (d.b_path or d.a_path or "").replace("\\", "/")
                        if not include_gitops and rel_path.startswith(".alfredo_gitops/"):
                            continue
                        status = "modified"
                        if d.new_file:
                            status = "added"
                        elif d.deleted_file:
                            status = "deleted"
                        elif d.renamed_file:
                            status = "renamed"
                        changed_files.append({"path": rel_path, "status": status})

                        old_bytes = self._read_blob_bytes(base_sha, d.a_path or rel_path) if not d.new_file else b""
                        new_bytes = self._read_blob_bytes(to_sha, d.b_path or rel_path) if not d.deleted_file else b""
                        block = self._format_file_diff_block(rel_path, status, old_bytes, new_bytes)
                        if block:
                            diff_blocks.append(block)
                else:
                    # Compare base_commit with current working directory + untracked files
                    diffs = base_commit.diff(None)
                    seen_paths = set()
                    for d in diffs:
                        rel_path = (d.b_path or d.a_path or "").replace("\\", "/")
                        if not include_gitops and rel_path.startswith(".alfredo_gitops/"):
                            continue
                        seen_paths.add(rel_path)
                        abs_path = os.path.join(self.workspace_dir, rel_path)
                        if d.new_file:
                            status = "added"
                        elif d.deleted_file or not os.path.exists(abs_path):
                            status = "deleted"
                        else:
                            status = "modified"
                        changed_files.append({"path": rel_path, "status": status})

                        old_bytes = self._read_blob_bytes(base_sha, d.a_path or rel_path) if status != "added" else b""
                        new_bytes = b""
                        if status != "deleted" and os.path.exists(abs_path) and os.path.isfile(abs_path):
                            try:
                                with open(abs_path, "rb") as f:
                                    new_bytes = f.read()
                            except Exception:
                                pass
                        block = self._format_file_diff_block(rel_path, status, old_bytes, new_bytes)
                        if block:
                            diff_blocks.append(block)

                    for untracked in self.repo.untracked_files:
                        rel_path = untracked.replace("\\", "/")
                        if rel_path in seen_paths:
                            continue
                        if not include_gitops and rel_path.startswith(".alfredo_gitops/"):
                            continue
                        abs_path = os.path.join(self.workspace_dir, rel_path)
                        if not os.path.isfile(abs_path):
                            continue
                        changed_files.append({"path": rel_path, "status": "added"})
                        try:
                            with open(abs_path, "rb") as f:
                                new_bytes = f.read()
                        except Exception:
                            new_bytes = b""
                        block = self._format_file_diff_block(rel_path, "added", b"", new_bytes)
                        if block:
                            diff_blocks.append(block)

                if not changed_files:
                    return result

                result["has_changes"] = True
                result["changed_files"] = changed_files
                counts = {"added": 0, "modified": 0, "deleted": 0, "renamed": 0}
                for cf in changed_files:
                    counts[cf["status"]] = counts.get(cf["status"], 0) + 1
                parts = []
                if counts["added"]:
                    parts.append(f"+{counts['added']} creati")
                if counts["modified"]:
                    parts.append(f"~{counts['modified']} modificati")
                if counts["deleted"]:
                    parts.append(f"-{counts['deleted']} rimossi")
                files_preview = ", ".join(cf["path"] for cf in changed_files[:4])
                if len(changed_files) > 4:
                    files_preview += f" (+{len(changed_files) - 4} altri)"
                result["summary"] = f"{len(changed_files)} file toccati ({', '.join(parts)}): {files_preview}"

                combined_diff = "\n\n".join(diff_blocks)
                if len(combined_diff) > max_chars:
                    combined_diff = combined_diff[:max_chars] + "\n... [Diff troncato per leggibilità]"
                result["diff_text"] = combined_diff
                return result
            except Exception as e:
                result["summary"] = f"Impossibile calcolare il diff: {e}"
                return result

    def _format_file_diff_block(
        self,
        rel_path: str,
        status: str,
        old_bytes: Optional[bytes],
        new_bytes: Optional[bytes],
    ) -> str:
        ext = os.path.splitext(rel_path)[1].lower()
        old_lines = self._extract_readable_lines_from_bytes(old_bytes or b"", ext) if old_bytes else []
        new_lines = self._extract_readable_lines_from_bytes(new_bytes or b"", ext) if new_bytes else []

        icon = {"added": "🟢 [NUOVO]", "modified": "🟡 [MODIFICATO]", "deleted": "🔴 [ELIMINATO]"}.get(
            status, "🔵 [FILE]"
        )
        header = f"{icon} {rel_path}"

        if old_lines is None or new_lines is None:
            old_sz = len(old_bytes or b"")
            new_sz = len(new_bytes or b"")
            return f"{header} (File binario: {old_sz} B -> {new_sz} B)"

        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel_path}",
                tofile=f"b/{rel_path}",
                lineterm="",
                n=2,
            )
        )
        if not diff_lines:
            return f"{header} (Nessuna differenza testuale rilevata)"

        # Keep at most 45 lines per file so one large file doesn't crowd out others
        body = "\n".join(diff_lines[2:47])
        if len(diff_lines) > 47:
            body += f"\n... (+{len(diff_lines) - 47} righe di diff omesse)"
        return f"{header}\n```diff\n{body}\n```"

    # ------------------------------------------------------------------
    # Auto-Checkpointing per Workflow / Step
    # ------------------------------------------------------------------
    def checkpoint_step(
        self,
        run_id: Optional[int],
        step_label: str,
        agent_role: str = "System",
        task_name: str = "",
        pre_step_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Stages all workspace changes and creates an automatic Git checkpoint commit
        if any files were created, modified, or deleted during the step.
        Returns commit metadata + Smart Diff.
        """
        info: Dict[str, Any] = {
            "committed": False,
            "commit_sha": self.get_head_sha(),
            "short_sha": (self.get_head_sha() or "")[:8],
            "summary": "Nessun file modificato in questo step.",
            "diff_text": "",
            "changed_files": [],
        }
        if not self.enabled or not self.repo:
            return info

        with _GLOBAL_GIT_LOCK:
            try:
                base_sha = pre_step_sha or self.get_head_sha()
                diff_info = self.get_smart_diff(from_sha=base_sha, to_sha=None, include_gitops=False)
                if not diff_info["has_changes"]:
                    return info

                self.repo.git.add(A=True)
                # Verify index actually has staged changes compared to HEAD
                if self.repo.head.is_valid() and not self.repo.index.diff("HEAD"):
                    return info

                run_tag = f"Run #{run_id}" if run_id is not None else "Manual/Dynamic"
                clean_role = (agent_role or "Agent").strip().replace("\n", " ")[:50]
                clean_task = (task_name or step_label or "Step").strip().replace("\n", " ")[:70]
                commit_title = f"[{run_tag} | {step_label}] {clean_role}: {clean_task}"
                commit_body = (
                    f"{commit_title}\n\n"
                    f"Summary: {diff_info['summary']}\n"
                    f"Run-ID: {run_id if run_id is not None else 'none'}\n"
                    f"Step-Label: {step_label}\n"
                    f"Agent-Role: {clean_role}\n"
                    f"Timestamp: {datetime.now().isoformat()}\n"
                )
                commit = self.repo.index.commit(commit_body)
                info.update(
                    {
                        "committed": True,
                        "commit_sha": commit.hexsha,
                        "short_sha": commit.hexsha[:8],
                        "summary": diff_info["summary"],
                        "diff_text": diff_info["diff_text"],
                        "changed_files": diff_info["changed_files"],
                    }
                )
                return info
            except Exception as e:
                print(f"[GitWorkspace] Checkpoint warning: {e}")
                return info

    # ------------------------------------------------------------------
    # Time-Travel: Rollback & Checkpoint Listing
    # ------------------------------------------------------------------
    def list_checkpoints(self, limit: int = 35, include_gitops: bool = True) -> List[Dict[str, Any]]:
        """
        Returns recent Git checkpoints with commit SHA, timestamp, author, title, and changed files.
        """
        if not self.enabled or not self.repo:
            return []

        checkpoints: List[Dict[str, Any]] = []
        with _GLOBAL_GIT_LOCK:
            try:
                if not self.repo.head.is_valid():
                    return []
                for commit in self.repo.iter_commits("HEAD", max_count=limit * 2):
                    lines = (commit.message or "").strip().splitlines()
                    title = lines[0] if lines else commit.hexsha[:8]
                    if not include_gitops and title.startswith("[GitOps"):
                        continue

                    # Extract trailer metadata if present
                    meta: Dict[str, str] = {}
                    for line in lines[1:]:
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()

                    changed_files = []
                    try:
                        if commit.parents:
                            for d in commit.parents[0].diff(commit):
                                p = (d.b_path or d.a_path or "").replace("\\", "/")
                                changed_files.append(p)
                    except Exception:
                        pass

                    checkpoints.append(
                        {
                            "sha": commit.hexsha,
                            "short_sha": commit.hexsha[:8],
                            "title": title,
                            "summary": meta.get("Summary", ""),
                            "run_id": meta.get("Run-ID", ""),
                            "step_label": meta.get("Step-Label", ""),
                            "agent_role": meta.get("Agent-Role", ""),
                            "timestamp": datetime.fromtimestamp(commit.committed_date).strftime("%Y-%m-%d %H:%M:%S"),
                            "changed_files": changed_files,
                        }
                    )
                    if len(checkpoints) >= limit:
                        break
            except Exception as e:
                print(f"[GitWorkspace] list_checkpoints error: {e}")
        return checkpoints

    def rollback_to_commit(self, target_sha: str, reason: str = "User Time-Travel Rollback") -> Dict[str, Any]:
        """
        Non-destructively rolls back the workspace to the state at `target_sha` by:
        1. Auto-committing any dirty working-tree files first (so nothing is ever lost).
        2. Restoring the exact tree of `target_sha` into the workspace and committing it as a
           forward Time-Travel Rollback commit (preserving full forward/backward history!).
        """
        if not self.enabled or not self.repo:
            return {"success": False, "error": "GitWorkspaceManager non attivo."}

        with _GLOBAL_GIT_LOCK:
            try:
                # 1. Save dirty state if any before rolling back
                self.checkpoint_step(
                    run_id=None,
                    step_label="Pre-Rollback-Backup",
                    agent_role="GitTimeTravel",
                    task_name="Salvataggio automatico stato corrente prima del rollback",
                )

                target_commit = self.repo.commit(target_sha)
                target_short = target_commit.hexsha[:8]

                # 2. Remove tracked files not in target_commit (except .alfredo_gitops if desired)
                current_files = [
                    p.replace("\\", "/")
                    for p in self.repo.git.ls_files().splitlines()
                    if not p.replace("\\", "/").startswith(".alfredo_gitops/")
                ]
                target_files = [
                    item.path.replace("\\", "/")
                    for item in target_commit.tree.traverse()
                    if item.type == "blob" and not item.path.replace("\\", "/").startswith(".alfredo_gitops/")
                ]

                for f_path in current_files:
                    if f_path not in target_files and f_path != ".gitignore":
                        abs_f = os.path.join(self.workspace_dir, f_path)
                        if os.path.exists(abs_f):
                            os.remove(abs_f)

                # 3. Checkout all workspace files from target_commit
                for f_path in target_files:
                    self.repo.git.checkout(target_commit.hexsha, "--", f_path)

                self.repo.git.add(A=True)
                if self.repo.index.diff("HEAD"):
                    msg = (
                        f"[Time-Travel Rollback -> {target_short}] {reason}\n\n"
                        f"Summary: Ripristinato workspace al checkpoint {target_short}\n"
                        f"Target-Commit: {target_commit.hexsha}\n"
                        f"Timestamp: {datetime.now().isoformat()}\n"
                    )
                    new_commit = self.repo.index.commit(msg)
                    return {
                        "success": True,
                        "restored_to": target_short,
                        "new_commit_sha": new_commit.hexsha,
                        "new_short_sha": new_commit.hexsha[:8],
                        "message": f"Workspace ripristinato con successo al checkpoint {target_short}.",
                    }
                return {
                    "success": True,
                    "restored_to": target_short,
                    "new_commit_sha": self.get_head_sha(),
                    "new_short_sha": (self.get_head_sha() or "")[:8],
                    "message": f"Il workspace è già identico al checkpoint {target_short}.",
                }
            except Exception as e:
                return {"success": False, "error": f"Errore durante il rollback: {e}"}

    def restore_single_file(self, rel_path: str, target_sha: str) -> Dict[str, Any]:
        """Restores a single file in `workspace/` from a historical commit."""
        if not self.enabled or not self.repo:
            return {"success": False, "error": "GitWorkspaceManager non attivo."}

        with _GLOBAL_GIT_LOCK:
            try:
                clean_rel = rel_path.replace("\\", "/").lstrip("/")
                target_commit = self.repo.commit(target_sha)
                self.repo.git.checkout(target_commit.hexsha, "--", clean_rel)
                self.repo.git.add(clean_rel)
                if self.repo.index.diff("HEAD"):
                    c = self.repo.index.commit(
                        f"[Time-Travel File Restore] Ripristinato {clean_rel} da {target_commit.hexsha[:8]}"
                    )
                    return {
                        "success": True,
                        "commit_sha": c.hexsha,
                        "message": f"File '{clean_rel}' ripristinato dalla versione {target_commit.hexsha[:8]}.",
                    }
                return {
                    "success": True,
                    "commit_sha": self.get_head_sha(),
                    "message": f"File '{clean_rel}' era già identico alla versione {target_commit.hexsha[:8]}.",
                }
            except Exception as e:
                return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Parallel DAG Isolation via `git worktree`
    # ------------------------------------------------------------------
    def create_node_worktree(self, run_id: int, node_id: str) -> Optional[str]:
        """
        Creates a lightweight, isolated Git worktree for a parallel DAG node
        so concurrent agents do not overwrite each other's files mid-execution.
        """
        if not self.enabled or not self.repo:
            return None

        with _GLOBAL_GIT_LOCK:
            try:
                os.makedirs(self.worktrees_dir, exist_ok=True)
                safe_node = _slugify(f"run_{run_id}_{node_id}")
                wt_path = os.path.join(self.worktrees_dir, safe_node)
                if os.path.exists(wt_path):
                    self.cleanup_node_worktree(wt_path)

                head_sha = self.get_head_sha()
                if not head_sha:
                    return None
                self.repo.git.worktree("add", "--detach", wt_path, head_sha)
                return wt_path
            except Exception as e:
                print(f"[GitWorkspace] Worktree creation warning: {e}")
                return None

    def cleanup_node_worktree(self, wt_path: str) -> None:
        if not self.enabled or not self.repo or not wt_path:
            return
        with _GLOBAL_GIT_LOCK:
            try:
                self.repo.git.worktree("remove", "--force", wt_path)
            except Exception:
                shutil.rmtree(wt_path, ignore_errors=True)
                try:
                    self.repo.git.worktree("prune")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # GitOps: Automatic Versioning of Agents, Tasks & Workflows
    # ------------------------------------------------------------------
    def snapshot_ai_config(
        self,
        entity_type: str,
        entity_id: int,
        entity_name: str,
        payload: Dict[str, Any],
        reason: str = "Updated via Alfredo",
    ) -> Optional[str]:
        """
        Saves a structured JSON snapshot of an Agent, Task, or Workflow under
        `workspace/.alfredo_gitops/<entity_type>/` and commits it automatically.
        Provides a complete audit trail of prompt/backstory/workflow changes.
        """
        if not self.enabled or not self.repo:
            return None

        with _GLOBAL_GIT_LOCK:
            try:
                folder = os.path.join(self.gitops_dir, _slugify(entity_type))
                os.makedirs(folder, exist_ok=True)
                filename = f"{int(entity_id):04d}_{_slugify(entity_name)}.json"
                abs_path = os.path.join(folder, filename)
                rel_path = os.path.relpath(abs_path, self.workspace_dir).replace("\\", "/")

                snapshot_data = {
                    "entity_type": entity_type,
                    "entity_id": int(entity_id),
                    "entity_name": entity_name,
                    "updated_at": datetime.now().isoformat(),
                    "reason": reason,
                    "data": payload,
                }
                with open(abs_path, "w", encoding="utf-8") as f:
                    json.dump(snapshot_data, f, indent=2, ensure_ascii=False)

                self.repo.git.add(rel_path)
                if not self.repo.head.is_valid() or self.repo.index.diff("HEAD"):
                    msg = (
                        f"[GitOps | {entity_type.upper()} #{entity_id}] {entity_name} — {reason}\n\n"
                        f"Summary: Versioning automatico {entity_type} '{entity_name}' ({reason})\n"
                        f"Entity-Type: {entity_type}\n"
                        f"Entity-ID: {entity_id}\n"
                        f"Timestamp: {datetime.now().isoformat()}\n"
                    )
                    commit = self.repo.index.commit(msg)
                    return commit.hexsha
            except Exception as e:
                print(f"[GitWorkspace] GitOps snapshot warning: {e}")
        return None


# Singleton helper
_WORKSPACE_GIT_MANAGER: Optional[GitWorkspaceManager] = None


def get_git_workspace() -> GitWorkspaceManager:
    global _WORKSPACE_GIT_MANAGER
    if _WORKSPACE_GIT_MANAGER is None:
        _WORKSPACE_GIT_MANAGER = GitWorkspaceManager()
    return _WORKSPACE_GIT_MANAGER
