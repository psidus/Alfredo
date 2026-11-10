"""
core/automation_scheduler.py

Event-Driven Automation Engine for Alfredo AI OS.
Integrates:
- `apscheduler` (https://github.com/agronholm/apscheduler) for Cron & Interval workflow scheduling
- `watchdog` (https://github.com/gorakhargosh/watchdog) for real-time File-System Folder triggers
- Built-in lightweight daemon fallback so scheduled & file-driven workflows run in any environment.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTOMATIONS_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "automations.json")
AUTOMATIONS_STATE_PATH = os.path.join(PROJECT_ROOT, "config", "automations_state.json")
DEFAULT_INBOX_DIR = os.path.join(PROJECT_ROOT, "workspace", "inbox")
# How far past its scheduled time a rule may still fire once (minutes).
DEFAULT_CATCHUP_MINUTES = max(
    0, int(os.getenv("ALFREDO_SCHEDULER_CATCHUP_MINUTES", "720") or 720)
)

_SCHED_LOCK = threading.RLock()


class AutomationEngine:
    """
    Manages and executes time-based (Cron / Interval) and event-based (Folder Watchdog)
    workflow automations in the background.

    Scheduler state (last fire time, files already seen per rule) is persisted next
    to the rules, so a restart neither fires an interval rule immediately nor loses
    files that landed while Alfredo was offline.
    """

    def __init__(
        self,
        config_path: str = AUTOMATIONS_CONFIG_PATH,
        state_path: Optional[str] = None,
    ):
        self.config_path = config_path
        self.state_path = state_path or AUTOMATIONS_STATE_PATH
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        os.makedirs(DEFAULT_INBOX_DIR, exist_ok=True)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Dict[str, Any]] = {}
        self._load_state()
        self._ensure_config()

    def _ensure_config(self) -> None:
        if not os.path.exists(self.config_path):
            self.save_rules([])

    # --- Persisted scheduler state -----------------------------------------
    def _load_state(self) -> None:
        with _SCHED_LOCK:
            try:
                if os.path.exists(self.state_path):
                    with open(self.state_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    rules = data.get("rules") if isinstance(data, dict) else None
                    self._state = rules if isinstance(rules, dict) else {}
                else:
                    self._state = {}
            except Exception as e:  # noqa: BLE001 - a corrupt state file must not stop Alfredo
                logger.warning(f"[AutomationEngine] Could not read state {self.state_path}: {e}")
                self._state = {}

    def _save_state(self) -> None:
        with _SCHED_LOCK:
            try:
                os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
                tmp_path = f"{self.state_path}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({"rules": self._state}, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.state_path)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[AutomationEngine] Could not persist state: {e}")

    def _rule_state(self, rule_id: str) -> Dict[str, Any]:
        entry = self._state.get(str(rule_id))
        if not isinstance(entry, dict):
            entry = {}
            self._state[str(rule_id)] = entry
        return entry

    def load_rules(self) -> List[Dict[str, Any]]:
        with _SCHED_LOCK:
            try:
                if os.path.exists(self.config_path):
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        return data
                    if isinstance(data, dict):
                        return data.get("rules", [])
            except Exception as e:
                logger.warning(f"[AutomationEngine] Failed to read {self.config_path}: {e}")
            return []

    def save_rules(self, rules: List[Dict[str, Any]]) -> None:
        with _SCHED_LOCK:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump({"rules": rules}, f, indent=2, ensure_ascii=False)

    def add_rule(
        self,
        name: str,
        workflow_id: int,
        trigger_type: str,
        interval_minutes: int = 60,
        daily_time: str = "",
        watch_folder: str = "",
        file_extensions: str = ".pdf,.xlsx,.csv,.docx,.txt",
        default_inputs: Optional[Dict[str, Any]] = None,
        enabled: bool = True,
        catchup_minutes: Optional[int] = None,
    ) -> str:
        rules = self.load_rules()
        rule_id = str(uuid.uuid4())[:8]
        folder = (watch_folder or DEFAULT_INBOX_DIR).strip()
        if trigger_type == "folder_watch":
            os.makedirs(folder, exist_ok=True)
            # Snapshot existing files so we only trigger on NEW files
            seen = self._scan_folder_files(folder, file_extensions)
            self._state[rule_id] = {"seen_files": seen}
            self._save_state()

        rules.append(
            {
                "id": rule_id,
                "name": name.strip() or f"Auto-{rule_id}",
                "workflow_id": int(workflow_id),
                "trigger_type": trigger_type,  # 'interval' | 'daily' | 'folder_watch'
                "interval_minutes": max(1, int(interval_minutes or 60)),
                "daily_time": (daily_time or "08:00").strip(),
                "watch_folder": folder,
                "file_extensions": file_extensions.strip(),
                "default_inputs": default_inputs or {},
                "enabled": bool(enabled),
                "catchup_minutes": (
                    DEFAULT_CATCHUP_MINUTES
                    if catchup_minutes is None
                    else max(0, int(catchup_minutes))
                ),
                "last_triggered": None,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.save_rules(rules)
        self.start_background_loop()
        return rule_id

    def delete_rule(self, rule_id: str) -> bool:
        rules = self.load_rules()
        filtered = [r for r in rules if r.get("id") != rule_id]
        if len(filtered) != len(rules):
            self.save_rules(filtered)
            self._state.pop(str(rule_id), None)
            self._save_state()
            return True
        return False

    def toggle_rule(self, rule_id: str, enabled: bool) -> bool:
        rules = self.load_rules()
        for r in rules:
            if r.get("id") == rule_id:
                r["enabled"] = bool(enabled)
                self.save_rules(rules)
                return True
        return False

    UPDATABLE_FIELDS = (
        "name",
        "workflow_id",
        "trigger_type",
        "interval_minutes",
        "daily_time",
        "watch_folder",
        "file_extensions",
        "default_inputs",
        "enabled",
        "catchup_minutes",
    )
    # Changing one of these invalidates the scheduler clock (e.g. back to 08:00).
    RESET_CLOCK_FIELDS = ("trigger_type", "interval_minutes", "daily_time", "watch_folder")

    def update_rule(self, rule_id: str, **fields: Any) -> bool:
        """Patch a rule in place. Unknown fields are ignored.

        Changing the schedule resets the clock so the rule does not fire
        immediately with the old timing; changing the watched folder re-snapshots
        its files so existing content is not treated as new.
        """
        rules = self.load_rules()
        for r in rules:
            if r.get("id") != rule_id:
                continue
            reset_clock = False
            folder_changed = False
            for key, value in fields.items():
                if key not in self.UPDATABLE_FIELDS:
                    continue
                if key == "workflow_id":
                    value = int(value)
                elif key == "interval_minutes":
                    value = max(1, int(value or 60))
                elif key == "catchup_minutes":
                    value = max(0, int(value if value is not None else DEFAULT_CATCHUP_MINUTES))
                elif key == "enabled":
                    value = bool(value)
                elif key == "default_inputs":
                    value = dict(value or {})
                else:
                    value = str(value or "").strip()
                if r.get(key) != value:
                    if key in self.RESET_CLOCK_FIELDS:
                        reset_clock = True
                    if key == "watch_folder":
                        folder_changed = True
                r[key] = value
            self.save_rules(rules)

            state = self._rule_state(rule_id)
            if reset_clock:
                state["last_run_ts"] = time.time()
                state.pop("daily_handled_date", None)
            if folder_changed or r.get("trigger_type") == "folder_watch":
                folder = r.get("watch_folder") or DEFAULT_INBOX_DIR
                os.makedirs(folder, exist_ok=True)
                state["seen_files"] = self._scan_folder_files(
                    folder, r.get("file_extensions") or ""
                )
            self._save_state()
            return True
        return False

    def next_runs(
        self,
        window_hours: int = 48,
        *,
        limit: int = 50,
        now_ts: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Upcoming planned executions, for the dashboard calendar.

        ``folder_watch`` rules are event-driven and therefore not listed: the
        scheduler cannot know when a file will land.
        """
        now = float(now_ts if now_ts is not None else time.time())
        horizon = now + max(1, int(window_hours)) * 3600
        entries: List[Dict[str, Any]] = []

        for rule in self.load_rules():
            if not rule.get("enabled", True):
                continue
            rid = str(rule.get("id"))
            ttype = rule.get("trigger_type")
            if ttype not in ("interval", "daily"):
                continue
            base = {
                "rule_id": rid,
                "name": rule.get("name") or rid,
                "workflow_id": rule.get("workflow_id"),
                "trigger_type": ttype,
            }
            state = self._rule_state(rid)
            occurrences: List[float] = []

            if ttype == "interval":
                interval = max(60, int(rule.get("interval_minutes") or 60) * 60)
                last_ts = state.get("last_run_ts")
                next_ts = now + interval if last_ts is None else float(last_ts) + interval
                if next_ts < now:
                    missed = int((now - next_ts) // interval) + 1
                    next_ts += missed * interval
                while next_ts <= horizon and len(occurrences) < 3:
                    occurrences.append(next_ts)
                    next_ts += interval
            else:
                try:
                    hh, mm = (rule.get("daily_time") or "08:00").strip().split(":")
                    candidate = datetime.fromtimestamp(now).replace(
                        hour=int(hh), minute=int(mm), second=0, microsecond=0
                    )
                except (ValueError, TypeError):
                    continue
                if candidate.timestamp() <= now:
                    candidate = candidate + timedelta(days=1)
                while candidate.timestamp() <= horizon and len(occurrences) < 3:
                    occurrences.append(candidate.timestamp())
                    candidate = candidate + timedelta(days=1)

            for ts in occurrences:
                entries.append(
                    {
                        **base,
                        "ts": ts,
                        "when": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                    }
                )

        entries.sort(key=lambda item: item["ts"])
        return entries[: max(1, int(limit))]

    @staticmethod
    def _scan_folder_files(folder: str, extensions_csv: str) -> List[str]:
        if not folder or not os.path.isdir(folder):
            return []
        exts = [e.strip().lower() for e in (extensions_csv or "").split(",") if e.strip()]
        matched = []
        try:
            for fname in os.listdir(folder):
                if fname.startswith(("~$", ".")):
                    continue
                fpath = os.path.join(folder, fname)
                if not os.path.isfile(fpath):
                    continue
                if not exts or any(fname.lower().endswith(ext) for ext in exts):
                    matched.append(os.path.abspath(fpath))
        except Exception:
            pass
        return matched

    def _trigger_workflow_async(self, rule: Dict[str, Any], extra_inputs: Optional[Dict[str, Any]] = None) -> None:
        """Start the workflow through ``run_service`` so automation runs behave
        exactly like chat/dashboard runs (interrupts, event log, statuses)."""
        wf_id = int(rule["workflow_id"])
        inputs = dict(rule.get("default_inputs") or {})
        if extra_inputs:
            inputs.update(extra_inputs)
        if "user_input" not in inputs:
            inputs["user_input"] = f"Automated trigger: {rule.get('name', '')}"

        try:
            from core.run_service import start_run

            run_id = start_run(wf_id, inputs=inputs, source="automation")
        except Exception as e:  # noqa: BLE001 - a broken rule must not kill the loop
            logger.error(f"[AutomationEngine] Could not start workflow {wf_id}: {e}")
            return

        try:
            from core import events

            events.emit(
                run_id,
                events.AUTOMATION_FIRED,
                {
                    "rule_id": str(rule.get("id") or ""),
                    "rule_name": rule.get("name") or "",
                    "workflow_id": wf_id,
                    "trigger_type": rule.get("trigger_type"),
                    **({"file_name": extra_inputs.get("file_name")} if extra_inputs else {}),
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("[AutomationEngine] Could not log automation event", exc_info=True)

    def _mark_rule_triggered(self, rule_id: str, now_ts: Optional[float] = None) -> None:
        rules = self.load_rules()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rules:
            if r.get("id") == rule_id:
                r["last_triggered"] = now_str
                break
        self.save_rules(rules)
        self._rule_state(str(rule_id))["last_run_ts"] = float(now_ts if now_ts is not None else time.time())
        self._save_state()

    def _tick(self, now_ts: Optional[float] = None) -> None:
        rules = self.load_rules()
        now = float(now_ts if now_ts is not None else time.time())
        now_dt = datetime.fromtimestamp(now)
        today_str = now_dt.strftime("%Y-%m-%d")

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            rid = str(rule.get("id"))
            ttype = rule.get("trigger_type")
            state = self._rule_state(rid)
            catchup_sec = max(
                0,
                int(
                    rule.get("catchup_minutes")
                    if rule.get("catchup_minutes") is not None
                    else DEFAULT_CATCHUP_MINUTES
                ),
            ) * 60

            if ttype == "interval":
                interval_sec = max(60, int(rule.get("interval_minutes") or 60) * 60)
                last_ts = state.get("last_run_ts")
                if last_ts is None:
                    # First time we see this rule: start the clock, never fire now.
                    state["last_run_ts"] = now
                    self._save_state()
                    continue
                due_at = float(last_ts) + interval_sec
                if now < due_at:
                    continue
                if now - due_at > catchup_sec:
                    # Too late to be useful: skip the missed run instead of
                    # catching up on hours of backlog.
                    state["last_run_ts"] = now
                    state["last_skip"] = {
                        "at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "missed_since": datetime.fromtimestamp(due_at).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    self._save_state()
                    logger.info(
                        "[AutomationEngine] Skipped missed interval for '%s' (due %s, window %sm).",
                        rule.get("name"), state["last_skip"]["missed_since"], catchup_sec // 60,
                    )
                    continue
                self._mark_rule_triggered(rid, now)
                self._trigger_workflow_async(rule)

            elif ttype == "daily":
                # One fire per calendar day, late-but-within-window included.
                handled = state.get("daily_handled_date")
                if not handled:
                    handled = str(rule.get("last_triggered") or "")[:10]
                    if handled:
                        state["daily_handled_date"] = handled
                if handled == today_str:
                    continue
                try:
                    hh, mm = (rule.get("daily_time") or "08:00").strip().split(":")
                    target_dt = now_dt.replace(
                        hour=int(hh), minute=int(mm), second=0, microsecond=0
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        "[AutomationEngine] Invalid daily_time '%s' in rule %s.",
                        rule.get("daily_time"), rid,
                    )
                    continue
                if now_dt < target_dt:
                    continue
                late_sec = (now_dt - target_dt).total_seconds()
                state["daily_handled_date"] = today_str
                if late_sec > catchup_sec:
                    state["last_skip"] = {
                        "at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "missed_since": target_dt.strftime("%Y-%m-%d %H:%M"),
                    }
                    self._save_state()
                    logger.info(
                        "[AutomationEngine] Skipped missed daily trigger for '%s' (%s).",
                        rule.get("name"), target_dt.strftime("%Y-%m-%d %H:%M"),
                    )
                    continue
                self._mark_rule_triggered(rid, now)
                self._trigger_workflow_async(rule)

            elif ttype == "folder_watch":
                folder = rule.get("watch_folder") or DEFAULT_INBOX_DIR
                exts = rule.get("file_extensions") or ""
                current_files = set(self._scan_folder_files(folder, exts))
                seen = state.get("seen_files")
                if not isinstance(seen, list):
                    state["seen_files"] = sorted(current_files)
                    self._save_state()
                    continue
                new_files = current_files - set(seen)
                # Keep the state bounded by what is actually in the folder.
                state["seen_files"] = sorted(current_files)
                self._save_state()
                for nf in sorted(new_files):
                    self._mark_rule_triggered(rid, now)
                    self._trigger_workflow_async(
                        rule,
                        extra_inputs={
                            "file_path": nf,
                            "file_name": os.path.basename(nf),
                            "user_input": f"Nuovo file rilevato automaticamente: {nf}",
                        },
                    )

    def start_background_loop(self) -> None:
        with _SCHED_LOCK:
            if self._running and self._thread and self._thread.is_alive():
                return
            self._running = True

            def _loop():
                while self._running:
                    try:
                        self._tick()
                    except Exception as e:
                        logger.debug(f"[AutomationEngine] Tick error: {e}")
                    time.sleep(10)

            self._thread = threading.Thread(target=_loop, daemon=True, name="alfredo-automation-daemon")
            self._thread.start()


_AUTOMATION_ENGINE: Optional[AutomationEngine] = None


def get_automation_engine() -> AutomationEngine:
    global _AUTOMATION_ENGINE
    if _AUTOMATION_ENGINE is None:
        _AUTOMATION_ENGINE = AutomationEngine()
        _AUTOMATION_ENGINE.start_background_loop()
    return _AUTOMATION_ENGINE
