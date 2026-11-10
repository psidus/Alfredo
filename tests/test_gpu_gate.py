"""Tests for the GPU gate: FIFO queue, VRAM fit checks, preload and metrics."""

from __future__ import annotations

import subprocess
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core import gpu_gate
from core.gpu_gate import (
    GpuGate,
    GpuInsufficientVram,
    GpuProfile,
    GpuQueueTimeout,
)
from core.run_context import current_run_id, run_context

FAST = GpuProfile(name="fast", model="fast:8b", context_tokens=8192, estimated_vram_mb=6000)
DEEP = GpuProfile(name="deep", model="deep:14b", context_tokens=8192, estimated_vram_mb=10000)
VRAM_OK = [{"index": 0, "name": "RTX 3060", "total_mb": 12288, "used_mb": 1000, "free_mb": 11288}]
VRAM_LOW = [{"index": 0, "name": "RTX 3060", "total_mb": 12288, "used_mb": 5288, "free_mb": 7000}]


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class GateTestBase(unittest.TestCase):
    def make_gate(self, *, vram=None, preloader=None, reserve_mb=500):
        return GpuGate(
            {"fast": FAST, "deep": DEEP},
            reserve_mb=reserve_mb,
            max_wait_seconds=2,
            vram_probe=(lambda: vram) if vram is not None else (lambda: None),
            preloader=preloader if preloader is not None else (lambda profile: None),
        )


class AdmissionTests(GateTestBase):
    def test_acquire_release_moves_the_queue_in_fifo_order(self):
        gate = self.make_gate(vram=VRAM_OK)
        first = gate.acquire("fast", run_id=1)
        self.assertEqual(gate.status()["queue"]["current"]["run_id"], 1)

        order = []
        lock = threading.Lock()

        def worker(profile):
            ticket = gate.acquire(profile, timeout=5)
            with lock:
                order.append(profile)
            gate.release(ticket)

        deep = threading.Thread(target=worker, args=("deep",), daemon=True)
        deep.start()
        self.assertTrue(wait_until(lambda: gate.status()["queue"]["length"] == 1))
        fast = threading.Thread(target=worker, args=("fast",), daemon=True)
        fast.start()
        self.assertTrue(wait_until(lambda: gate.status()["queue"]["length"] == 2))

        waiting = gate.status()["queue"]["waiting"]
        self.assertEqual([item["profile"] for item in waiting], ["deep", "fast"])
        self.assertEqual([item["position"] for item in waiting], [1, 2])
        self.assertIsNotNone(waiting[0]["eta_s"])
        self.assertGreater(waiting[1]["eta_s"], waiting[0]["eta_s"])

        gate.release(first)
        deep.join(timeout=5)
        fast.join(timeout=5)
        self.assertEqual(order, ["deep", "fast"])

    def test_timeout_removes_the_ticket_and_counts_it(self):
        gate = self.make_gate(vram=VRAM_OK)
        first = gate.acquire("fast")
        try:
            with self.assertRaises(GpuQueueTimeout) as caught:
                gate.acquire("deep", timeout=0.05)
            self.assertIn("still waiting", str(caught.exception))
            self.assertEqual(gate.status()["queue"]["length"], 0)
            self.assertEqual(gate.status()["metrics"]["timeouts"], 1)
        finally:
            gate.release(first)

    def test_deep_profile_is_rejected_when_it_does_not_fit(self):
        gate = self.make_gate(vram=VRAM_LOW)
        with self.assertRaises(GpuInsufficientVram) as caught:
            gate.acquire("deep")
        message = str(caught.exception)
        self.assertIn("deep:14b", message)
        self.assertIn("fast", message)  # actionable alternative
        status = gate.status()
        self.assertEqual(status["metrics"]["rejected"], 1)
        self.assertIsNone(status["queue"]["current"])
        self.assertFalse(status["profiles"]["deep"]["fits"])
        self.assertTrue(status["profiles"]["fast"]["fits"])

    def test_unknown_gpu_allows_the_call(self):
        gate = self.make_gate(vram=None)
        ticket = gate.acquire("deep")
        try:
            status = gate.status()
            self.assertFalse(status["gpu"]["available"])
            self.assertIsNone(status["profiles"]["deep"]["fits"])
            self.assertEqual(status["queue"]["current"]["profile"], "deep")
        finally:
            gate.release(ticket)

    def test_custom_model_never_rejected(self):
        gate = self.make_gate(vram=VRAM_LOW)
        custom = gate.custom_profile("other:7b")
        self.assertEqual(custom.estimated_vram_mb, 0)
        ticket = gate.acquire(custom)
        gate.release(ticket)
        self.assertEqual(gate.status()["metrics"]["served"], 1)

    def test_release_is_idempotent(self):
        gate = self.make_gate(vram=VRAM_OK)
        ticket = gate.acquire("fast")
        gate.release(ticket)
        gate.release(ticket)
        gate.release(None)
        self.assertEqual(gate.status()["metrics"]["served"], 1)


class PreloadTests(GateTestBase):
    def test_preload_only_on_profile_switch(self):
        preloaded = []
        gate = self.make_gate(vram=VRAM_OK, preloader=preloaded.append)
        gate.release(gate.acquire("fast"))
        gate.release(gate.acquire("fast"))
        self.assertEqual(len(preloaded), 1)
        gate.release(gate.acquire("deep"))
        self.assertEqual([profile.name for profile in preloaded], ["fast", "deep"])
        self.assertEqual(gate.status()["metrics"]["evictions"], 1)
        self.assertEqual(gate.status()["loaded_model"], "deep:14b")

    def test_preload_failure_is_not_fatal(self):
        def broken(profile):
            raise RuntimeError("ollama down")

        gate = self.make_gate(vram=VRAM_OK, preloader=broken)
        ticket = gate.acquire("fast")
        gate.release(ticket)
        status = gate.status()
        self.assertIn("ollama down", status["preload_error"])

    def test_switch_is_reported_to_the_event_sink_only_inside_a_run(self):
        events = []
        gpu_gate.set_event_sink(lambda run_id, kind, payload: events.append((run_id, kind, payload)))
        self.addCleanup(gpu_gate.set_event_sink, None)
        gate = self.make_gate(vram=VRAM_OK)

        # Outside a run: no telemetry, even with a run id.
        gate.release(gate.acquire("fast", run_id=1))
        self.assertEqual(events, [])

        # Inside a run: the switch is attributed to that run.
        with run_context(7):
            gate.release(gate.acquire("deep", run_id=7))
        self.assertEqual(len(events), 1)
        run_id, kind, payload = events[0]
        self.assertEqual(run_id, 7)
        self.assertEqual(kind, "system.model.switch")
        self.assertEqual(payload["profile"], "deep")
        self.assertEqual(payload["model"], DEEP.model)

        # Same model again: no new switch event.
        with run_context(7):
            gate.release(gate.acquire("deep", run_id=7))
        self.assertEqual(len(events), 1)

    def test_event_sink_errors_never_break_acquire(self):
        def boom(*args, **kwargs):
            raise RuntimeError("db gone")

        gpu_gate.set_event_sink(boom)
        self.addCleanup(gpu_gate.set_event_sink, None)
        gate = self.make_gate(vram=VRAM_OK)
        with run_context(3):
            ticket = gate.acquire("fast", run_id=3)
        gate.release(ticket)
        self.assertEqual(gate.status()["metrics"]["served"], 1)


class MetricsTests(GateTestBase):
    def test_first_token_stats(self):
        gate = self.make_gate(vram=VRAM_OK)
        gate.record_first_token("fast", 0.5)
        gate.record_first_token(FAST, 1.5)
        gate.record_first_token("deep", None)
        stats = gate.status()["metrics"]["first_token"]
        self.assertEqual(stats["fast"]["count"], 2)
        self.assertEqual(stats["fast"]["avg_s"], 1.0)
        self.assertEqual(stats["fast"]["last_s"], 1.5)
        self.assertEqual(stats["fast"]["p95_s"], 1.5)
        self.assertNotIn("deep", stats)

    def test_model_mapping(self):
        gate = self.make_gate(vram=VRAM_OK)
        self.assertEqual(gate.profile_for_model("ollama/fast:8b").name, "fast")
        self.assertEqual(gate.profile_for_model("deep:14b").name, "deep")
        self.assertEqual(gate.profile_for_model("deep:7b").name, "deep")  # same family
        self.assertIsNone(gate.profile_for_model("other:7b"))
        self.assertIsNone(gate.profile_for_model(""))

    def test_status_shape(self):
        gate = self.make_gate(vram=VRAM_LOW)
        ticket = gate.acquire("fast", run_id=7)
        try:
            status = gate.status()
            self.assertTrue(status["gpu"]["available"])
            self.assertEqual(status["gpu"]["free_mb"], 7000)
            self.assertEqual(status["gpu"]["reserve_mb"], 500)
            self.assertIsNotNone(status["profiles"]["fast"]["headroom_mb"])
            self.assertIsNotNone(status["profiles"]["fast"]["avg_call_s"])
            self.assertEqual(status["queue"]["current"]["run_id"], 7)
            self.assertIsNotNone(status["queue"]["current"]["started_at"])
            self.assertEqual(status["queue"]["length"], 0)
            self.assertIn("served", status["metrics"])
        finally:
            gate.release(ticket)

    def test_wait_time_is_recorded(self):
        gate = self.make_gate(vram=VRAM_OK)
        first = gate.acquire("fast")
        result = {}
        def waiter():
            ticket = gate.acquire("deep", timeout=5)
            result["ticket"] = ticket
        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        self.assertTrue(wait_until(lambda: gate.status()["queue"]["length"] == 1))
        time.sleep(0.05)
        gate.release(first)
        thread.join(timeout=5)
        gate.release(result.get("ticket"))
        avg_wait = gate.status()["metrics"]["avg_wait_s"]
        self.assertIsNotNone(avg_wait)


class RunContextTests(unittest.TestCase):
    def test_run_context_round_trip(self):
        self.assertIsNone(current_run_id())
        with run_context(42):
            self.assertEqual(current_run_id(), 42)
        self.assertIsNone(current_run_id())

    def test_acquire_attributes_the_run(self):
        gate = GpuGate(
            {"fast": FAST, "deep": DEEP},
            reserve_mb=500,
            vram_probe=lambda: VRAM_OK,
            preloader=lambda profile: None,
        )
        with run_context(99):
            ticket = gate.acquire("fast", run_id=current_run_id())
        try:
            self.assertEqual(gate.status()["queue"]["current"]["run_id"], 99)
        finally:
            gate.release(ticket)


class ProbeTests(unittest.TestCase):
    def test_parses_nvidia_smi(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout="0, NVIDIA GeForce RTX 3060, 12288, 1000, 11288\n",
            stderr="",
        )
        with patch.object(subprocess, "run", return_value=completed):
            gpus = gpu_gate.probe_nvidia_smi()
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 3060")
        self.assertEqual(gpus[0]["free_mb"], 11288)

    def test_missing_nvidia_smi_is_none(self):
        with patch.object(subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(gpu_gate.probe_nvidia_smi())
        completed = SimpleNamespace(returncode=1, stdout="", stderr="no devices")
        with patch.object(subprocess, "run", return_value=completed):
            self.assertIsNone(gpu_gate.probe_nvidia_smi())


class WiringTests(unittest.TestCase):
    def setUp(self):
        from pathlib import Path

        self.root = Path(__file__).resolve().parents[1]

    def source(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def test_master_ai_goes_through_the_gate(self):
        source = self.source("core/master_ai.py")
        self.assertIn("gpu_gate.GPU_GATE", source)
        self.assertIn("gate.acquire(profile", source)
        self.assertIn("gate.release(ticket)", source)
        self.assertIn("record_first_token", source)
        self.assertIn('"stream": True', source)
        self.assertNotIn("_local_llm_slot", source)

    def test_run_service_binds_the_run_context(self):
        source = self.source("core/run_service.py")
        self.assertIn("run_context.run_context(run_id)", source)

    def test_api_exposes_system_status(self):
        source = self.source("core/api_server.py")
        self.assertIn('"/api/system/status"', source)
        self.assertIn('"/api/v1/system/status"', source)

    def test_docs_mention_the_two_profiles(self):
        source = self.source("core/gpu_gate.py")
        self.assertIn("ALFREDO_GPU_QUEUE_TIMEOUT", source)
        self.assertIn("GpuInsufficientVram", source)
        self.assertIn("ALFREDO_GPU_RESERVE_MB", source)


if __name__ == "__main__":
    unittest.main()
