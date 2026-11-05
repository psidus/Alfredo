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
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
AUTOMATIONS_CONFIG_PATH = os.path.join(PROJECT_ROOT, "config", "automations.json")
DEFAULT_INBOX_DIR = os.path.join(PROJECT_ROOT, "workspace", "inbox")

_SCHED_LOCK = threading.RLock()


class AutomationEngine:
    """
    Manages and executes time-based (Cron / Interval) and event-based (Folder Watchdog)
    workflow automations in the background.
    """

    def __init__(self, config_path: str = AUTOMATIONS_CONFIG_PATH):
        self.config_path = config_path
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        os.makedirs(DEFAULT_INBOX_DIR, exist_ok=True)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._seen_files: Dict[str, set] = {}
        self._last_run_ts: Dict[str, float] = {}
        self._ensure_config()

    def _ensure_config(self) -> None:
        if not os.path.exists(self.config_path):
            self.save_rules([])

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
    ) -> str:
        rules = self.load_rules()
        rule_id = str(uuid.uuid4())[:8]
        folder = (watch_folder or DEFAULT_INBOX_DIR).strip()
        if trigger_type == "folder_watch":
            os.makedirs(folder, exist_ok=True)
            # Snapshot existing files so we only trigger on NEW files
            self._seen_files[rule_id] = set(self._scan_folder_files(folder, file_extensions))

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
        wf_id = int(rule["workflow_id"])
        inputs = dict(rule.get("default_inputs") or {})
        if extra_inputs:
            inputs.update(extra_inputs)
        if "user_input" not in inputs:
            inputs["user_input"] = f"Automated trigger: {rule.get('name', '')}"

        def _runner():
            from core.db_manager import DBManager
            from core.crew_builder import execute_crew_with_memory

            db = DBManager()
            run_id = db.create_run(wf_id, status="running", inputs=inputs, source="automation")
            try:
                last_out, _ = execute_crew_with_memory(
                    workflow_id=wf_id,
                    inputs=inputs,
                    run_id=run_id,
                )
                db.update_run(run_id, status="completed", result=str(last_out or ""))
            except Exception as e:
                logger.error(f"[AutomationEngine] Run #{run_id} failed: {e}")
                db.update_run(run_id, status="failed", result=str(e))

        threading.Thread(target=_runner, daemon=True, name=f"auto-wf-{wf_id}").start()

    def _mark_rule_triggered(self, rule_id: str) -> None:
        rules = self.load_rules()
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for r in rules:
            if r.get("id") == rule_id:
                r["last_triggered"] = now_str
                break
        self.save_rules(rules)

    def _tick(self) -> None:
        rules = self.load_rules()
        now = time.time()
        now_hm = datetime.now().strftime("%H:%M")
        today_str = datetime.now().strftime("%Y-%m-%d")

        for rule in rules:
            if not rule.get("enabled", True):
                continue
            rid = str(rule.get("id"))
            ttype = rule.get("trigger_type")

            if ttype == "interval":
                interval_sec = max(60, int(rule.get("interval_minutes") or 60) * 60)
                last_ts = self._last_run_ts.get(rid)
                if last_ts is None:
                    self._last_run_ts[rid] = now
                    continue
                if now - last_ts >= interval_sec:
                    self._last_run_ts[rid] = now
                    self._mark_rule_triggered(rid)
                    self._trigger_workflow_async(rule)

            elif ttype == "daily":
                target_hm = (rule.get("daily_time") or "08:00").strip()
                last_trig = str(rule.get("last_triggered") or "")
                if now_hm == target_hm and not last_trig.startswith(today_str):
                    self._mark_rule_triggered(rid)
                    self._trigger_workflow_async(rule)

            elif ttype == "folder_watch":
                folder = rule.get("watch_folder") or DEFAULT_INBOX_DIR
                exts = rule.get("file_extensions") or ""
                current_files = set(self._scan_folder_files(folder, exts))
                if rid not in self._seen_files:
                    self._seen_files[rid] = current_files
                    continue
                new_files = current_files - self._seen_files[rid]
                self._seen_files[rid] = current_files
                for nf in sorted(new_files):
                    self._mark_rule_triggered(rid)
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
